"""End-to-end alternative-source pipeline: simulated Shield -> real API over TLS -> real workers/PyAV ->
two separate local 'CDN' origins (playback source A, thumbnail source B).

Fixtures: tests/fixtures/gen_master.py (run gen_altsrc.py; see fixtures/README.md) (ONE deterministic master per film; every
positive variant is a controlled transformation of that exact master; negatives are other masters or reorderings).
Directory: $OPENSEEK_TEST_FIXTURES. Ground truth is the frame-number barcode burned into every
frame of the master (it survives every derivation), so it reads the PLAYBACK timeline directly: A time of any
frame = N/24 s, for every variant (the `off` candidate's 7 s intro carries master B's own numbering and is never
a tile source; `pal` frames keep master numbering).

Matching variants run on the 10-minute set (long enough for the alternative source to matter); the negative
variants use the 120-second set. Correctness tests force negotiation (alt_min_gain_s=0); the break-even rule has
its own test."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from helpers import FIXTURES, FIXTURES_HINT, decode_barcode, make_policy, parse_vtt, wait_for
from origin import Origin

FIX = FIXTURES
LONG, SHORT = "600s", "120s"
pytestmark = pytest.mark.skipif(not (FIX / f"play_{LONG}.mkv").exists() or not (FIX / f"play_{SHORT}.mkv").exists(),
                                reason=FIXTURES_HINT)

HA, HB = "a.cdn.example", "b.cdn.example"


def dur_ms(suf: str) -> int:
    return int(suf[:-1]) * 1000


def play_meta(suf: str) -> dict:
    return dict(cid="play", title_key="tt1", height=2160, duration_ms=dur_ms(suf), size=dur_ms(suf) * 750,
                codec="hevc", container="mkv", name="Film.2160p.REMUX", cached=True, resolved=False)


def cand(cid: str, suf: str, variant: str = "ok", **kw) -> dict:
    d = dict(cid=cid, title_key="tt1", height=720,
             duration_ms={"ok": dur_ms(suf), "off": dur_ms(suf) + 7000, "pal": dur_ms(suf) * 24 // 25,
                          "ins": dur_ms(suf) + 10_000, "swap": dur_ms(suf), "other": dur_ms(suf)}[variant],
             size=dur_ms(suf) * 625, codec="h264", container="mkv", name="Film.720p.BluRay.x264", cached=True,
             resolved=False)
    d.update(kw)
    return d


class Rig:
    def __init__(self, live_factory, min_gain: float = 0.0, **over):
        self.oa, self.ob = Origin(FIX).start(), Origin(FIX).start()
        self.oa.reset(latency_s=0.35)           # remote 4K source: slow-ish round trips (integration only)
        self.ob.reset(latency_s=0.05)
        self.srv = live_factory(make_policy([self.oa, self.ob], {HA: ["127.0.0.1"], HB: ["127.0.0.1"]}),
                                alt_source=True, workers_per_source=3, alt_max_conc=3, alt_mbps=400, alt_rps=60,
                                alt_min_gain_s=min_gain, **over)
        token, _ = self.srv.pair()
        self.c = self.srv.client(token)
        self.stop_tel = threading.Event()

    def url(self, which: str, name: str) -> str:
        o, h = (self.oa, HA) if which == "a" else (self.ob, HB)
        return f"http://{h}:{o.port}/f/{name}"

    def create(self, suf: str, candidates: list[dict], telemetry: bool = True) -> dict:
        r = self.c.post("/v1/previews", json={"source": {"url": self.url("a", f"play_{suf}.mkv")}, "playhead_ms": 0,
                                              "layout": "tiles",
                                              "alt": {"play": play_meta(suf), "candidates": candidates,
                                                      "telemetry": telemetry}})
        assert r.status_code == 202, r.text
        return r.json()

    def status(self, pid: str) -> dict:
        r = self.c.get(f"/v1/previews/{pid}")
        assert r.status_code == 200, r.text
        return r.json()

    def telemetry(self, pid: str) -> None:
        self.stop_tel.clear()

        def loop():
            while not self.stop_tel.is_set():
                try:
                    self.c.post(f"/v1/previews/{pid}/telemetry",
                                json={"buffer_ms": 20_000, "rebuffering": False, "host": HB, "conns": 1})
                except Exception:  # noqa: BLE001
                    pass
                self.stop_tel.wait(0.4)
        threading.Thread(target=loop, daemon=True).start()

    def wait_want(self, pid: str, cid: str = "c1") -> None:
        assert wait_for(lambda: (self.status(pid).get("alt") or {}).get("want") == cid, 40, 0.1)

    def offer(self, pid: str, cid: str, name: str) -> int:
        return self.c.post(f"/v1/previews/{pid}/alt-source",
                           json={"cid": cid, "source": {"url": self.url("b", name)}}).status_code

    def settle_alt(self, pid: str, timeout: float = 120.0) -> dict:
        """Wait until validation finished: the alternative source is active, or the attempt was rejected."""
        def settled():
            s = self.status(pid)
            a = s.get("alt") or {}
            if a.get("state") == "active" or (a.get("state") in ("wanted", "rejected", "failed") and a.get("reason")):
                return s
            return None
        return wait_for(settled, timeout, 0.2)

    def complete(self, pid: str, timeout: float = 240.0) -> dict:
        def done():
            s = self.status(pid)
            return s if s["progress"]["tiles_total"] and s["progress"]["tiles_done"] >= s["progress"]["tiles_total"] else None
        return wait_for(done, timeout, 0.25)

    def tiles(self, pid: str) -> list[tuple[int, int, int | None]]:
        """(k, vtt_t_ms, barcode_frame) for every tile."""
        r = self.c.get(self.status(pid)["vtt_url"])
        assert r.status_code == 200
        out = []
        for _, _, url in parse_vtt(r.text):
            path = urlsplit(url).path
            if "/tiles/" not in path:
                continue
            k = int(path.rsplit("/", 1)[1][:-4])
            t = int(url.split("&t=")[1].split("#")[0])
            jpg = self.c.get(url)
            assert jpg.status_code == 200
            out.append((k, t, decode_barcode(jpg.content)))
        return sorted(out)

    def source(self):
        return next(iter(self.srv.core.jobs.sources.values()))

    def b_requests(self, name: str) -> int:
        return sum(1 for r in self.ob.log if r["path"].endswith(name) and r["status"] == 206)

    def a_requests(self) -> int:
        return sum(1 for r in self.oa.log if r["status"] == 206)

    def close(self) -> None:
        self.stop_tel.set()
        self.c.close()
        self.oa.stop()
        self.ob.stop()


@pytest.fixture()
def rig(live_factory):
    r = Rig(live_factory)
    yield r
    r.close()


def accuracy(tiles) -> tuple[int, list[int]]:
    """Missing/undecodable count and per-tile error (ms) of the true playback time (barcode) vs the 10 s slot."""
    errs, missing = [], 0
    for k, _, n in tiles:
        if n is None:
            missing += 1
            continue
        errs.append(int(n / 24 * 1000) - k * 10_000)
    return missing, errs


@pytest.mark.parametrize("variant,offset", [("ok", 0), ("off", 7000), ("pal", 0)])
def test_matching_release_is_validated_used_and_accurate(rig, variant, offset):
    name = f"{variant}_{LONG}.mkv"
    t0 = time.perf_counter()
    body = rig.create(LONG, [cand("c1", LONG, variant)])
    pid = body["preview_id"]
    rig.telemetry(pid)
    rig.wait_want(pid)                                            # exactly ONE resolution requested
    assert rig.offer(pid, "c1", name) == 202
    s = rig.settle_alt(pid)
    assert s["alt"]["state"] == "active", s["alt"]
    done = rig.complete(pid)
    elapsed = time.perf_counter() - t0
    tiles = rig.tiles(pid)
    n_tiles = dur_ms(LONG) // 10_000
    assert len(tiles) == done["progress"]["tiles_total"] == n_tiles
    alt_slots = {e.k for e in rig.source().events if e.via_alt}
    missing, errs = accuracy(tiles)
    assert missing == 0 and max(abs(e) for e in errs) <= 5000, (missing, max(abs(e) for e in errs))
    meta = rig.srv.core.jobs.jobs[pid].alt
    print(f"\n[{variant}] elapsed={elapsed:.1f}s via_alt={len(alt_slots)}/{n_tiles} validation={meta.validation} "
          f"max_err={max(abs(e) for e in errs)}ms A_reqs={rig.a_requests()} B_reqs={rig.b_requests(name)}")
    assert alt_slots, "the alternative source never produced a tile"
    assert rig.srv.core.db.get_thumb_source(rig.source().fp) is not None   # provenance recorded, A fp untouched
    assert meta.spot.get("failed", 0) <= 1                        # closed-loop spot check ran and passed


@pytest.mark.parametrize("variant", ["other", "ins", "swap"])
def test_mismatched_release_is_rejected_and_priority_c_completes(rig, variant):
    name = f"{variant}_{SHORT}.mkv"
    body = rig.create(SHORT, [cand("c1", SHORT, variant)])
    pid = body["preview_id"]
    rig.telemetry(pid)
    rig.wait_want(pid)
    assert rig.offer(pid, "c1", name) == 202
    s = rig.settle_alt(pid)
    assert s["alt"]["state"] != "active" and s["alt"]["reason"]
    rig.complete(pid)
    missing, errs = accuracy(rig.tiles(pid))
    assert missing == 0 and max(abs(e) for e in errs) <= 5000     # fallback tiles are the real playback source
    assert not any(e.via_alt for e in rig.source().events)


def test_no_telemetry_means_no_alt_negotiation(rig):
    body = rig.create(SHORT, [cand("c1", SHORT)], telemetry=False)
    assert "alt" not in body                                       # backward compatible response
    assert rig.offer(body["preview_id"], "c1", f"ok_{SHORT}.mkv") == 409
    rig.complete(body["preview_id"])


def test_ineligible_candidates_are_never_wanted(rig):
    body = rig.create(SHORT, [cand("big", SHORT, height=2160), cand("uncached", SHORT, cached=False),
                              cand("ext", SHORT, name="Film.EXTENDED.720p"), cand("other", SHORT, title_key="tt9")])
    time.sleep(2)
    assert rig.status(body["preview_id"])["alt"]["want"] is None


def test_legacy_client_without_alt_block_unchanged(rig):
    r = rig.c.post("/v1/previews", json={"source": {"url": rig.url("a", f"play_{SHORT}.mkv")}, "layout": "tiles"})
    assert r.status_code == 202 and "alt" not in r.json()
    rig.complete(r.json()["preview_id"])


def test_break_even_skips_negotiation_when_playback_source_finishes_first(live_factory):
    """Default threshold + a short film: the playback source completes long before validation could pay off, so
    no link is requested and no validation traffic is spent on the candidate."""
    r = Rig(live_factory, min_gain=8.0)
    try:
        r.oa.reset(latency_s=0.02)                                 # fast, equal conditions: nothing to gain
        r.ob.reset(latency_s=0.02)
        body = r.create(SHORT, [cand("c1", SHORT)])
        pid = body["preview_id"]
        r.telemetry(pid)
        r.complete(pid)
        a = r.status(pid)["alt"]
        assert a["want"] is None and a["state"] in ("none", "wanted")
        assert r.offer(pid, "c1", f"ok_{SHORT}.mkv") == 409
        assert r.b_requests(f"ok_{SHORT}.mkv") == 0                # not one request spent on the alternative
    finally:
        r.close()


def test_expired_alt_link_falls_back_to_playback_source(rig):
    body = rig.create(LONG, [cand("c1", LONG)])
    pid = body["preview_id"]
    rig.telemetry(pid)
    rig.wait_want(pid)
    assert rig.offer(pid, "c1", f"ok_{LONG}.mkv") == 202
    assert rig.settle_alt(pid)["alt"]["state"] == "active"
    rig.ob.reset(expired=True)                                     # B's signed link dies; A is untouched
    rig.complete(pid)
    missing, errs = accuracy(rig.tiles(pid))
    assert missing == 0 and max(abs(e) for e in errs) <= 5000
    assert wait_for(lambda: rig.status(pid)["alt"]["state"] in ("failed", "active"), 30, 0.2)


def test_stale_telemetry_pauses_alt_extraction_playback_first(rig):
    body = rig.create(LONG, [cand("c1", LONG)])
    pid = body["preview_id"]
    rig.telemetry(pid)
    rig.wait_want(pid)
    assert rig.offer(pid, "c1", f"ok_{LONG}.mkv") == 202
    assert rig.settle_alt(pid)["alt"]["state"] == "active"
    rig.stop_tel.set()                                             # client stops reporting
    time.sleep(12)                                                 # > telemetry_stale_s
    gov = rig.source().governor
    assert gov.allowed(HB) <= 1                                    # at most the single safe slot, never more
    rig.telemetry(pid)                                             # telemetry returns: extraction resumes
    rig.complete(pid)


def test_cancellation_during_validation_leaves_nothing_running(rig):
    body = rig.create(LONG, [cand("c1", LONG)])
    pid = body["preview_id"]
    rig.telemetry(pid)
    rig.wait_want(pid)
    assert rig.offer(pid, "c1", f"ok_{LONG}.mkv") == 202
    r = rig.c.delete(f"/v1/previews/{pid}")
    assert r.status_code == 204
    jobs = rig.srv.core.jobs
    assert wait_for(lambda: all(not s.running for s in jobs.sources.values()), 60, 0.2)
    time.sleep(1.5)
    assert wait_for(lambda: all(not s.running for s in jobs.sources.values()), 10, 0.2)


def test_offer_is_refused_while_playback_health_is_unknown(rig):
    """No fresh telemetry => the server must not add probe traffic on the playback host, even if the client
    opted in and the candidate is eligible."""
    body = rig.create(LONG, [cand("c1", LONG)])
    pid = body["preview_id"]
    # the server no longer even ASKS for a link without fresh healthy telemetry (see jobs.alt_view), so `want` stays
    # empty while the job runs, and an unsolicited offer is refused
    time.sleep(2.0)
    assert (rig.status(pid).get("alt") or {}).get("want") is None
    assert rig.offer(pid, "c1", f"ok_{LONG}.mkv") == 409            # refused: no telemetry yet
    assert rig.b_requests(f"ok_{LONG}.mkv") == 0
    rig.telemetry(pid)
    assert wait_for(lambda: rig.offer(pid, "c1", f"ok_{LONG}.mkv") == 202, 20, 0.3)
    rig.complete(pid)


def test_malformed_alt_block_never_breaks_the_preview_request(rig):
    r = rig.c.post("/v1/previews", json={"source": {"url": rig.url("a", f"play_{SHORT}.mkv")}, "layout": "tiles",
                                         "alt": {"play": {"cid": 5}, "candidates": "nope"}})
    assert r.status_code == 202 and "alt" not in r.json()
    rig.complete(r.json()["preview_id"])


def _metrics(rig) -> dict:
    with rig.srv.admin_client() as a:
        return a.get("/admin/metrics").json()


def test_dry_run_reports_eligible_candidates_and_never_resolves_a_link(live_factory):
    """Metadata-only negotiation: the server ranks and reports, asks for nothing, contacts no candidate."""
    r = Rig(live_factory, alt_dry_run=True)
    try:
        body = r.create(LONG, [cand("c1", LONG), cand("big", LONG, height=2160), cand("uncached", LONG, cached=False)])
        pid = body["preview_id"]
        r.telemetry(pid)
        time.sleep(4)
        a = r.status(pid)["alt"]
        assert a["want"] is None and a["state"] == "dry_run"
        assert r.offer(pid, "c1", f"ok_{LONG}.mkv") == 409
        m = _metrics(r)["sources"][0]["alt"]
        by = {c["cid"]: c for c in m["candidates"]}
        assert by["c1"]["eligible"] and by["big"]["reject"] == "resolution" and by["uncached"]["reject"] == "not_cached"
        assert "url" not in str(m).lower() and m["dry_run"] is True
        assert r.b_requests(f"ok_{LONG}.mkv") == 0                  # not one request to any candidate
        r.complete(pid)
    finally:
        r.close()


def test_resolution_ledger_blocks_further_link_resolutions(live_factory):
    r = Rig(live_factory, alt_max_resolutions_5min=1)
    try:
        body = r.create(LONG, [cand("c1", LONG), cand("c2", LONG)])
        pid = body["preview_id"]
        r.telemetry(pid)
        r.wait_want(pid, "c1")
        # c1's link turns out to be the playback file itself: rejected after ONE resolution of that link
        assert r.offer(pid, "c1", f"play_{LONG}.mkv") == 202
        r.settle_alt(pid)
        assert wait_for(lambda: (r.status(pid).get("alt") or {}).get("want") == "c2", 30, 0.2)
        assert r.offer(pid, "c2", f"ok_{LONG}.mkv") == 409                # the ledger: no more this 5 minutes
        m = _metrics(r)["sources"][0]["alt"]
        assert m["resolutions_5min"] >= 1 and m["resolution_limit_5min"] == 1
        assert r.b_requests(f"ok_{LONG}.mkv") == 0                        # the second link was never touched
        r.complete(pid)
    finally:
        r.close()


def test_metrics_measure_resolutions_latency_concurrency_and_playback_health(rig):
    body = rig.create(LONG, [cand("c1", LONG)])
    pid = body["preview_id"]
    rig.telemetry(pid)
    rig.wait_want(pid)
    assert rig.offer(pid, "c1", f"ok_{LONG}.mkv") == 202
    assert rig.settle_alt(pid)["alt"]["state"] == "active"
    rig.complete(pid)
    src = _metrics(rig)["sources"][0]
    assert src["upstream"]["resolutions"] == 1                          # playback source: one resolution of its link
    alt = src["alt"]
    assert alt["upstream"]["resolutions"] == 1                          # the alternative: ONE, never per thumbnail
    assert alt["upstream"]["requests"] > 50 and alt["upstream"]["latency_ms_p50"] is not None
    assert alt["upstream"]["max_inflight"] >= 1 and alt["upstream"]["rate_limited"] == 0
    g = alt["governor"]
    assert g["reports"] > 3 and g["min_buffer_ms"] == 20000 and g["rebuffer_events"] == 0
    assert alt["attempts"] and alt["attempts"][0]["resolutions"] >= 1
    assert "http" not in str(alt)                                       # no URL anywhere in the diagnostics


def test_a_resent_candidate_list_replaces_the_stale_one_until_validation_starts(rig):
    body = rig.create(SHORT, [cand("old1", SHORT, height=2160)])
    pid = body["preview_id"]
    time.sleep(1.5)
    m = _metrics(rig)["sources"][0]["alt"]
    assert [c["cid"] for c in m["candidates"]] == ["old1"]
    rig.create(SHORT, [cand("new1", SHORT), cand("new2", SHORT, height=1080)])    # same URL, same device: same job
    time.sleep(1.5)
    m = _metrics(rig)["sources"][0]["alt"]
    assert sorted(c["cid"] for c in m["candidates"]) == ["new1", "new2"]
    rig.complete(pid)


def test_shared_caps_use_one_request_bucket_for_both_sources(live_factory):
    r = Rig(live_factory, alt_shared_caps=True, upstream_rps=5.0)
    try:
        jobs = r.srv.core.jobs
        assert jobs.alt_request_bucket is jobs.request_bucket and jobs.alt_bucket is jobs.bucket   # one budget in total
    finally:
        r.close()
    r2 = Rig(live_factory, upstream_rps=5.0)
    try:
        jobs = r2.srv.core.jobs
        assert jobs.alt_request_bucket is not jobs.request_bucket                                  # default: separate
    finally:
        r2.close()


def test_a_rejected_candidate_is_held_until_released(live_factory):
    r = Rig(live_factory, alt_hold_after_reject=True, alt_report_names=True)
    try:
        body = r.create(LONG, [cand("c1", LONG), cand("c2", LONG)])
        pid = body["preview_id"]
        r.telemetry(pid)
        r.wait_want(pid, "c1")
        assert r.offer(pid, "c1", f"play_{LONG}.mkv") == 202            # the playback file itself: rejected after preflight
        assert wait_for(lambda: (r.status(pid).get("alt") or {}).get("state") == "held", 60, 0.2)
        a = r.status(pid)["alt"]
        assert a["want"] is None and a["reason"]                          # nothing is offered while a person decides
        assert r.offer(pid, "c2", f"ok_{LONG}.mkv") == 409
        rec = _metrics(r)["sources"][0]["alt"]["attempts"][0]
        assert rec["outcome"] == "rejected" and rec["range_ok"] is True and rec["cid"] == "c1" and rec["name"]
        with r.srv.admin_client() as a_:
            assert a_.post("/admin/alt/release").json()["released"] == 1
        assert wait_for(lambda: (r.status(pid).get("alt") or {}).get("want") == "c2", 30, 0.2)
        assert r.offer(pid, "c2", f"ok_{LONG}.mkv") == 202                # now the second candidate may be tried
        r.complete(pid)
    finally:
        r.close()


def test_total_request_rate_across_both_sources_never_exceeds_the_shared_cap(live_factory):
    """The condition for the live stage: validation + extraction on the playback source AND the alternative together
    stay within ONE request budget (here 4/s), measured at the two origins."""
    cap = 4.0
    r = Rig(live_factory, alt_shared_caps=True, upstream_rps=cap)
    try:
        r.oa.reset(latency_s=0.02)
        r.ob.reset(latency_s=0.02)
        body = r.create(LONG, [cand("c1", LONG)])
        pid = body["preview_id"]
        r.telemetry(pid)
        r.wait_want(pid)
        assert r.offer(pid, "c1", f"ok_{LONG}.mkv") == 202
        assert r.settle_alt(pid, timeout=150)["alt"]["state"] == "active"
        r.complete(pid, timeout=240)
        times = sorted(x["t"] for o in (r.oa, r.ob) for x in o.log if x["status"] in (200, 206, 302))
        assert len(times) > 100
        worst = 0
        for i, t0 in enumerate(times):
            n = sum(1 for t in times[i:] if t - t0 <= 5.0)
            worst = max(worst, n)
        print(f"\nshared cap {cap}/s: most requests in any 5 s window = {worst} (limit {int(cap * 5) + 2}); total {len(times)}")
        assert worst <= int(cap * 5) + 2
        assert r.b_requests(f"ok_{LONG}.mkv") > 20                       # the alternative really was used
    finally:
        r.close()


def test_a_candidate_is_only_requested_once_telemetry_is_healthy(rig):
    """The client treats a refused offer as final, so the server must not ask before it can accept the answer."""
    body = rig.create(LONG, [cand("c1", LONG)])
    pid = body["preview_id"]
    time.sleep(5)                                                        # no telemetry yet
    assert rig.status(pid)["alt"]["want"] is None
    rig.telemetry(pid)
    rig.wait_want(pid)                                                   # asked only once the job's telemetry is fresh
    assert rig.offer(pid, "c1", f"ok_{LONG}.mkv") == 202
    assert rig.settle_alt(pid)["alt"]["state"] == "active"
    rig.complete(pid)


def test_buffering_after_a_scrub_or_skip_is_classified_as_a_seek_not_a_stall(rig):
    body = rig.create(LONG, [cand("c1", LONG)])
    pid = body["preview_id"]
    rig.telemetry(pid)
    for _ in range(14):                                       # steady playback first
        rig.c.post(f"/v1/previews/{pid}/telemetry", json={"rebuffering": False, "host": HB, "conns": 1})
    rig.stop_tel.set()
    assert rig.c.post(f"/v1/previews/{pid}/priority", json={"at_ms": 900_000, "playhead_ms": 100_000}).status_code == 200
    rig.c.post(f"/v1/previews/{pid}/telemetry", json={"rebuffering": True, "host": HB, "conns": 1})
    g = _metrics(rig)["sources"][0]["alt"]["governor"]
    assert g["seek_buffering_events"] == 1 and g["rebuffer_events"] == 0
    time.sleep(16)                                             # a long time after the seek: now it is a real stall
    rig.c.post(f"/v1/previews/{pid}/telemetry", json={"rebuffering": False, "host": HB, "conns": 1})
    for _ in range(12):
        rig.c.post(f"/v1/previews/{pid}/telemetry", json={"rebuffering": False, "host": HB, "conns": 1})
    rig.c.post(f"/v1/previews/{pid}/telemetry", json={"rebuffering": True, "host": HB, "conns": 1})
    g = _metrics(rig)["sources"][0]["alt"]["governor"]
    assert g["rebuffer_events"] == 1
    rig.complete(pid)
