"""Multi-range keyframe prefetch (default OFF): same tiles as unchanged Priority C, fewer CDN requests, safe fallbacks."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from helpers import wait_for
from origin import Origin
from openseek.fetch import ByteRangesParser, MultiRangeError
from openseek.scheduler import Scheduler
from test_integration import _create, _policy, _status, _url
from test_scenarios import _register

from helpers import FIXTURES as ALT, FIXTURES_HINT
FILE = "ok_10min.mkv"
needs_fixture = pytest.mark.skipif(not (ALT / FILE).exists(), reason=FIXTURES_HINT)


@pytest.fixture()
def origin():
    o = Origin(ALT).start()
    yield o
    o.stop()


def generate(live_factory, origin, *, prefetch: bool, workers: int = 2, mode: str = "ok", latency: float = 0.0,
             rps: float = 0.0, stop_after_parts: int | None = None, batch: int = 8, timeout: float = 120.0) -> dict:
    """Run one full grid through the real server; return tiles, origin counters, timings and proxy counters."""
    origin.reset(multirange=mode, latency_s=latency)
    srv = live_factory(_policy(origin), pregen="full", workers_per_source=workers, multirange_prefetch=prefetch,
                       multirange_batch=batch, upstream_rps=rps, create_wait_s=10.0)
    token, _ = _register(srv)
    t0 = time.perf_counter()
    first = None
    with srv.client(token) as c:
        body = _create(c, _url(origin, FILE), at_ms=0, layout="tiles")
        pid = body["preview_id"]
        jobs = srv.core.jobs
        if stop_after_parts is not None:
            wait_for(lambda: any(s.proxy_src and s.proxy_src.multi_parts >= stop_after_parts for s in jobs.sources.values()), 60, 0.02)
            for s in list(jobs.sources.values()):
                s.stop("test", wait=True)
            time.sleep(0.5)
        else:
            def done():
                nonlocal first
                st = _status(c, pid)
                if first is None and st["progress"]["tiles_done"] > 0:
                    first = time.perf_counter() - t0
                return st if st["state"] == "complete" else None
            wait_for(done, timeout, 0.05)
        elapsed = time.perf_counter() - t0
        src = next(iter(jobs.sources.values()))
        n = src.scheduler.n
        tiles = {}
        for k in range(n):
            blob = jobs.cache.tile_bytes(src.fp, k)
            if blob is not None:
                tiles[k] = blob
        e = jobs.cache.entry(src.fp)
        pts = {k: v for k, v in (e.tiles.items() if e else [])}
        ps = src.proxy_src
        out = {"tiles": tiles, "pts": pts, "n": n, "elapsed": elapsed, "first_tile_s": first,
               "origin_requests": origin.file_requests, "origin_multi": origin.multi_requests, "origin_bytes": origin.bytes,
               "log": list(origin.log), "events": list(src.events),
               "proxy": {"multi_requests": ps.multi_requests, "multi_parts": ps.multi_parts, "multi_fallbacks": ps.multi_fallbacks,
                         "hits": ps.prefetch_hits, "waits": ps.prefetch_waits, "enabled": ps.multi_enabled,
                         "pending": len(ps._pending)}}
    return out


_BASE: dict = {}


def _baseline(live_factory, origin) -> dict:
    """Unchanged Priority C, generated once per test session (the reference every other run is compared against)."""
    if "v" not in _BASE:
        _BASE["v"] = generate(live_factory, origin, prefetch=False)
    return _BASE["v"]


def _ranges(log):
    singles, multis = [], []
    for r in log:
        h = r.get("range") or ""
        if not h.startswith("bytes="):
            continue
        parts = [tuple(int(x) for x in p.split("-")) for p in h[6:].split(",") if p.split("-")[0] != ""]
        (multis if len(parts) > 1 else singles).append(parts)
    return singles, [p for m in multis for p in m]


@needs_fixture
def test_prefetch_gives_identical_tiles_with_fewer_requests_and_no_redundant_fetches(live_factory, origin):
    base = _baseline(live_factory, origin)
    pre = generate(live_factory, origin, prefetch=True)
    assert base["n"] == pre["n"] and len(base["tiles"]) == base["n"] == len(pre["tiles"])
    assert base["tiles"] == pre["tiles"]                                  # byte-identical JPEGs for every slot
    assert base["pts"] == pre["pts"]                                      # same presentation timestamps
    assert pre["origin_multi"] > 0 and base["origin_multi"] == 0
    assert pre["origin_requests"] < 0.5 * base["origin_requests"]         # the CDN sees far fewer requests
    assert pre["proxy"]["hits"] > 0
    singles, multi_windows = _ranges(pre["log"])
    inside = [s for ss in singles for s in ss if any(w0 <= s[0] and s[1] <= w1 for w0, w1 in multi_windows)]
    assert not inside, f"{len(inside)} single-range requests re-fetched bytes a multi-range request already carried"
    assert pre["proxy"]["pending"] == 0
    assert pre["origin_bytes"] <= 1.2 * base["origin_bytes"]              # same bytes, plus multipart framing/alignment


@needs_fixture
@pytest.mark.parametrize("mode", ["whole", "single", "drop", "bad", "garbage", "truncate", "off"])
def test_fallbacks_keep_priority_c_correct(live_factory, origin, mode):
    base = _baseline(live_factory, origin)
    pre = generate(live_factory, origin, prefetch=True, mode=mode)
    assert pre["tiles"] == base["tiles"] and pre["pts"] == base["pts"]     # every tile still produced, identical
    assert pre["proxy"]["pending"] == 0
    if mode in ("whole", "single", "garbage", "off"):
        assert pre["proxy"]["enabled"] is False                            # unsupported: stopped asking
        assert pre["origin_multi"] <= 3


@needs_fixture
def test_stopping_mid_prefetch_leaves_a_consistent_cache_and_no_pending_windows(live_factory, origin):
    base = _baseline(live_factory, origin)
    cut = generate(live_factory, origin, prefetch=True, workers=3, latency=0.05, stop_after_parts=3)
    assert cut["proxy"]["pending"] == 0
    for k, blob in cut["tiles"].items():                                   # whatever was finished is exactly right
        assert blob == base["tiles"][k]
    assert len(cut["tiles"]) < base["n"]


@needs_fixture
def test_concurrent_workers_share_the_cache_without_corruption(live_factory, origin):
    base = _baseline(live_factory, origin)
    pre = generate(live_factory, origin, prefetch=True, workers=3, latency=0.02, batch=6)
    assert pre["tiles"] == base["tiles"]
    assert pre["proxy"]["pending"] == 0 and pre["proxy"]["multi_requests"] >= 2


# ----------------------------------------------------------------------------- units
def test_lookahead_matches_next_order_and_claims_nothing():
    a = Scheduler(60, "full", clock=lambda: 1e9)
    b = Scheduler(60, "full", clock=lambda: 1e9)
    ahead = a.lookahead(8)
    assert a.inflight_tasks == {}
    got = []
    for _ in range(8):
        t = b.next()
        got.append(t)
        b.complete(t, ok=True)
    assert [t.k for t in ahead] == [t.k for t in got][:len(ahead)] or {t.k for t in ahead} == {t.k for t in got}
    assert len({t.k for t in ahead}) == len(ahead)


def test_lookahead_never_returns_a_claimed_task_and_puts_p0_first():
    s = Scheduler(60, "full", clock=lambda: 1e9)
    s.set_priority(300_000, 0)
    first = s.next()
    assert first.cls == "P0"
    ahead = s.lookahead(5)
    assert first.k not in {t.k for t in ahead}


def _mp(parts, total, boundary="BND"):
    body = b""
    for (a, b), data in parts:
        body += f"--{boundary}\r\nContent-Type: x/y\r\nContent-Range: bytes {a}-{b}/{total}\r\n\r\n".encode() + data + b"\r\n"
    return body + f"--{boundary}--\r\n".encode()


def test_parser_handles_any_chunking_and_returns_parts_as_they_complete():
    ranges = [(0, 9), (100, 104), (500, 519)]
    parts = [((a, b), bytes([i]) * (b - a + 1)) for i, (a, b) in enumerate(ranges)]
    body = _mp(parts, 1000)
    for step in (1, 7, 64, len(body)):
        p = ByteRangesParser("BND", ranges, 1000)
        got = []
        for i in range(0, len(body), step):
            got += p.feed(body[i:i + step])
        assert [(a, b, d) for a, b, d in got] == [(a, b, d) for (a, b), d in parts] and p.done
    p = ByteRangesParser("BND", ranges, 1000)
    first = p.feed(body[:body.index(b"--BND", 5) + 40])
    assert first and first[0][:2] == (0, 9)                                  # first part usable before the rest arrived


@pytest.mark.parametrize("bad", ["range", "total", "repeat", "nocr", "length"])
def test_parser_rejects_anything_unexpected(bad):
    ranges = [(0, 9), (100, 104)]
    if bad == "range":
        body = _mp([((0, 9), b"a" * 10), ((100, 105), b"b" * 6)], 1000)
    elif bad == "total":
        body = _mp([((0, 9), b"a" * 10)], 999)
    elif bad == "repeat":
        body = _mp([((0, 9), b"a" * 10), ((0, 9), b"a" * 10)], 1000)
    elif bad == "nocr":
        body = b"--BND\r\nContent-Type: x/y\r\n\r\nxxxx\r\n--BND--\r\n"
    else:
        body = _mp([((0, 9), b"a" * 9)], 1000)
    p = ByteRangesParser("BND", ranges, 1000)
    with pytest.raises(MultiRangeError):
        p.feed(body)


def test_multirange_is_off_by_default_and_only_the_flag_turns_it_on():
    from openseek.config import ConfigError, Settings
    assert Settings().multirange_prefetch is False
    assert Settings.from_env({}).multirange_prefetch is False
    assert Settings.from_env({"OPENSEEK_MULTIRANGE_PREFETCH": "0"}).multirange_prefetch is False
    on = Settings.from_env({"OPENSEEK_MULTIRANGE_PREFETCH": "1"})
    assert on.multirange_prefetch is True and on.multirange_batch == 8
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENSEEK_MULTIRANGE_BATCH": "99"})
