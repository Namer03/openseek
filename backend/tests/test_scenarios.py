"""More end-to-end scenarios (real uvicorn + TLS, real worker subprocess, real PyAV)."""
from __future__ import annotations

import io
import time

import httpx
import psutil
import pytest
from PIL import Image

from helpers import FPS, decode_barcode, make_policy, parse_vtt, wait_for
from test_integration import HOST, _create, _policy, _status, _tiles_in_vtt, _url


def _job_src(srv, pid):
    jobs = srv.core.jobs
    job = jobs.jobs[pid].resolve()
    return job, jobs.sources.get(job.fp)


def _worker_pids() -> list[int]:
    """Worker subprocesses of this process. On Windows a venv python.exe is a launcher that runs
    the real interpreter as its own child with the same command line: count the outer one only."""
    found = {}
    for p in psutil.Process().children(recursive=True):
        try:
            if any("openseek.worker" in a for a in p.cmdline()):
                found[p.pid] = p.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return [pid for pid, parent in found.items() if parent not in found]


def _tile_url(vtt_url: str, k: int) -> str:
    exp = vtt_url.split("exp=")[1].split("&")[0]
    return vtt_url.split("/thumbs.vtt")[0] + f"/tiles/{k}.jpg?exp={exp}"


def _register(srv, name="tv"):
    from openseek.pairing import new_token, token_hash
    t = new_token()
    return t, srv.core.auth.register(name, token_hash(t))


def anon():
    return httpx.Client(verify=False)


def test_priority_change_mid_job(origin, live_factory, monkeypatch):
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        pid = _create(c, _url(origin, "main.mkv"), at_ms=0, playhead_ms=0)["preview_id"]
        job, src = _job_src(srv, pid)
        wait_for(lambda: len(src.events) >= 2)
        applied = []
        original_priority = srv.core.jobs.priority

        def record_priority(*args, **kwargs):
            # Count at server application, not before the HTTP round trip: the
            # worker can legitimately finish more frames while that request travels.
            with srv.core.jobs.lock, src.lock:
                applied.append(len(src.events))
                return original_priority(*args, **kwargs)

        monkeypatch.setattr(srv.core.jobs, "priority", record_priority)
        r = c.post(f"/v1/previews/{pid}/priority", json={"at_ms": 110_000, "playhead_ms": 0})
        assert r.status_code == 200 and set(r.json()) == {"vtt_version", "state"}
        wait_for(lambda: any(e.k == 11 for e in src.events))
        ks = [e.k for e in src.events]
        assert ks.index(11) <= applied[0] + 1    # next frame after the in-flight one
        assert next(e for e in src.events if e.k == 11).cls == "P0"


def test_cancel_stops_worker_tiles_stay(origin, live_factory):
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        body = _create(c, _url(origin, "main.mkv"), at_ms=30_000)
        pid = body["preview_id"]
        job, src = _job_src(srv, pid)
        wait_for(lambda: len(src.events) >= 1 and src.worker_pid)
        pid_w = src.worker_pid
        assert c.delete(f"/v1/previews/{pid}").status_code == 204
        wait_for(lambda: not psutil.pid_exists(pid_w), timeout=10)
        assert not src.running and _worker_pids() == []
        assert c.get(f"/v1/previews/{pid}").status_code == 404
        with anon() as a:
            assert a.get(_tile_url(body["vtt_url"], 3)).status_code == 200    # finished tiles stay


def test_worker_kill_recovery(origin, live_factory):
    # Latency keeps the 12-tile job running long enough that the kill lands mid-work; without it
    # the job could finish first, leaving nothing to restart for (restarts only count on a failed
    # grab), which made this test flaky (~1 in 5 runs).
    origin.reset(latency_s=0.15)
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        pid = _create(c, _url(origin, "main.mkv"), at_ms=0)["preview_id"]
        job, src = _job_src(srv, pid)
        wait_for(lambda: len(src.events) >= 2 and src.worker_pid)
        assert _status(c, pid)["progress"]["tiles_done"] < 12, "job finished before the kill"
        psutil.Process(src.worker_pid).kill()             # simulated decoder crash
        st = wait_for(lambda: (s := _status(c, pid))["state"] == "complete" and s, timeout=60)
        assert st["error"] is None and src.restarts >= 1
        assert st["progress"]["tiles_done"] == 12


def test_restart_with_cache_persistence(origin, live_factory, tmp_path):
    data = tmp_path / "persist"
    srv = live_factory(_policy(origin), data_dir=data, pregen="off")
    token, _ = _register(srv)
    with srv.client(token) as c:
        b1 = _create(c, _url(origin, "main.mkv"), at_ms=50_000)
        wait_for(lambda: _status(c, b1["preview_id"])["progress"]["tiles_done"] >= 12)
        s1 = _status(c, b1["preview_id"])
    srv.stop()
    origin.reset()
    srv2 = live_factory(_policy(origin), data_dir=data, pregen="off")
    with srv2.client(token) as c:                         # token survived the restart (hash on disk)
        b2 = _create(c, _url(origin, "main.mkv") + "?rotated=1", at_ms=None)
        assert b2["preview_id"] == b1["preview_id"]        # same device + content -> same id
        assert b2["state"] in ("ready", "complete")
        assert b2["progress"]["tiles_done"] >= s1["progress"]["tiles_done"]
        assert b2["source_duration_ms"] == s1["source_duration_ms"]
        assert b2["vtt_version"] >= s1["vtt_version"]      # monotonic across restarts
        old_path = b1["vtt_url"].split(f":{srv.port}")[1]
        with httpx.Client(verify=False, base_url=srv2.base) as a:
            r = a.get(old_path)                            # caps survive (secret persisted)
            assert r.status_code == 200 and r.text.count("-->") >= s1["progress"]["tiles_done"]


def test_expired_link_mid_job(origin, live_factory):
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        origin.reset(redirects={"/r/main.mkv": "/f/main.mkv?sig=abc"})
        pid = _create(c, f"http://{HOST}:{origin.port}/r/main.mkv", at_ms=0)["preview_id"]
        wait_for(lambda: _status(c, pid)["progress"]["tiles_done"] >= 1)
        origin.cfg.expired = True                          # the signed link dies mid-job
        st = wait_for(lambda: (s := _status(c, pid))["state"] == "failed" and s, timeout=30)
        assert st["error"] == "source_expired"
        assert _tiles_in_vtt(c, st["vtt_url"])             # published tiles are still served
        assert sum(r["path"] == "/r/main.mkv" for r in origin.log) >= 2   # one re-resolution was tried


@pytest.mark.parametrize("name,setup,code,max_bytes", [
    ("main.mkv", {"range": False}, "range_unsupported", 262_144),
    ("nocues.mkv", {}, "no_index", 400_000),
    ("random.bin", {}, "not_video", 400_000),
    ("page.html", {}, "not_video", 400_000),
])
def test_rejections_with_bounded_bytes(origin, live_factory, name, setup, code, max_bytes):
    srv = live_factory(_policy(origin))
    token, _ = _register(srv)
    origin.reset(**setup)
    with srv.client(token) as c:
        body = _create(c, _url(origin, name))
        st = body if body["state"] == "failed" else wait_for(
            lambda: (s := _status(c, body["preview_id"]))["state"] == "failed" and s)
        assert st["error"] == code
    assert origin.bytes <= max_bytes, origin.bytes
    assert _worker_pids() == []


def test_concurrency_bound_429(origin, live_factory):
    srv = live_factory(_policy(origin), pregen="full", max_active_sources=2)
    toks = [_register(srv, f"tv{i}")[0] for i in range(3)]
    codes = []
    for t, n in zip(toks, ["main.mkv", "main.mp4", "gop11.mkv"]):
        with srv.client(t) as c:
            r = c.post("/v1/previews", json={"source": {"url": _url(origin, n)}, "at_ms": 0})
            codes.append(r.status_code)
            if r.status_code == 429:
                assert r.json() == {"error": "busy"} and int(r.headers["retry-after"]) > 0
    assert codes == [202, 202, 429]
    peak = 0
    for _ in range(40):
        peak = max(peak, len(_worker_pids()))
        time.sleep(0.05)
    assert 1 <= peak <= 2


def test_byte_budget_enforced(origin, live_factory):
    budget = 1 << 20
    srv = live_factory(_policy(origin), pregen="full", job_bytes=budget)
    token, _ = _register(srv)
    with srv.client(token) as c:
        pid = _create(c, _url(origin, "main.mkv"), at_ms=0)["preview_id"]
        st = wait_for(lambda: (s := _status(c, pid))["state"] == "failed" and s, timeout=60)
        assert st["error"] == "budget_exceeded"
    assert origin.bytes <= budget, origin.bytes


def test_long_gop_accurate_reextract(origin, live_factory):
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        body = _create(c, _url(origin, "gop11.mkv"), at_ms=20_000, layout="tiles")
        st = wait_for(lambda: (s := _status(c, body["preview_id"]))["state"] == "complete" and s, timeout=90)
        tiles = _tiles_in_vtt(c, st["vtt_url"])
        assert len(tiles) == 6
        with anon() as a:
            for k, t in tiles.items():
                frame = decode_barcode(a.get(_tile_url(st["vtt_url"], k)).content)
                assert frame is not None and abs(frame * 1000 / FPS - k * 10_000) <= 5_000
                assert abs(frame * 1000 / FPS - t) <= 1000 / FPS + 1
        _, src = _job_src(srv, body["preview_id"])
        assert any(e.accurate for e in src.events)


@pytest.mark.parametrize("name,box", [("main.mp4", (0, 0, 320, 180)), ("pillar.mkv", (40, 0, 240, 180)),
                                      ("hdr.mkv", (0, 0, 320, 180))])
def test_formats_geometry_and_hdr(origin, live_factory, name, box):
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        body = _create(c, _url(origin, name), at_ms=0, layout="tiles")
        st = wait_for(lambda: (s := _status(c, body["preview_id"]))["state"] == "complete" and s, timeout=60)
        tiles = _tiles_in_vtt(c, st["vtt_url"])
        assert tiles
        with anon() as a:
            for k, t in tiles.items():
                data = a.get(_tile_url(st["vtt_url"], k)).content
                im = Image.open(io.BytesIO(data)).convert("RGB")
                assert im.size == (320, 180)
                frame = decode_barcode(data, box)
                assert frame is not None and abs(frame * 1000 / FPS - t) <= 1000 / FPS + 1, (k, frame, t)
                if name == "pillar.mkv":
                    assert max(im.getpixel((5, 90))) < 30 and max(im.getpixel((315, 120))) < 30   # black bars
                if name == "hdr.mkv":
                    r, g, b = im.getpixel((160, 120))
                    assert max(r, g, b) - min(r, g, b) > 40       # tonemapped colour, not washed-out grey


def test_hybrid_and_sheets_layouts(origin, live_factory):
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        body = _create(c, _url(origin, "main.mkv"), at_ms=0)
        wait_for(lambda: _status(c, body["preview_id"])["state"] == "complete", timeout=60)
        with anon() as a:
            for layout in ("hybrid", "sheets"):
                cues = parse_vtt(a.get(body["vtt_url"].replace("layout=hybrid", f"layout={layout}")).text)
                assert len(cues) == 12 and all("/sheets/0.jpg" in u for _, _, u in cues)
                r = a.get(cues[0][2].split("#")[0])
                assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
                assert Image.open(io.BytesIO(r.content)).size == (3200, 1800)


def test_corrupt_tile_regenerated(origin, live_factory):
    srv = live_factory(_policy(origin), pregen="full")
    token, _ = _register(srv)
    with srv.client(token) as c:
        body = _create(c, _url(origin, "main.mkv"), at_ms=0)
        wait_for(lambda: _status(c, body["preview_id"])["state"] == "complete", timeout=60)
        f = next(srv.core.cache.root.rglob("tiles/4.jpg"))
        f.write_bytes(f.read_bytes()[:300])
        with anon() as a:
            r = a.get(_tile_url(body["vtt_url"], 4))
            assert r.status_code == 404 and r.json() == {"error": "not_found"}
            wait_for(lambda: a.get(_tile_url(body["vtt_url"], 4)).status_code == 200, timeout=30)


def test_api_no_plain_http(origin, live_factory):
    srv = live_factory(_policy(origin))
    with httpx.Client() as plain:
        try:
            r = plain.get(f"http://127.0.0.1:{srv.port}/v1/health")
            assert r.status_code != 200
        except httpx.HTTPError:
            pass


def test_logs_and_disk_redacted(origin, live_factory):
    import logging

    from openseek.logutil import setup_logging
    marker = "zq9marker7x"
    host = f"{marker}.example.com"
    buf = io.StringIO()
    setup_logging("DEBUG", stream=buf)
    try:
        pol = make_policy([origin], {host: ["127.0.0.1"]})
        srv = live_factory(pol, pregen="full")
        token, _ = srv.pair()
        origin.reset(redirects={f"/{marker}/r": f"/f/main.mkv?sig={marker}"})
        with srv.client(token) as c:
            r = c.post("/v1/previews", json={"source": {"url": f"http://{host}:{origin.port}/{marker}/r?k={marker}",
                                                        "headers": {"Referer": f"https://{marker}/",
                                                                    "User-Agent": marker, "Cookie": marker}},
                                             "hints": {"imdb_id": marker, "filename": marker}})
            body = r.json()
            st = wait_for(lambda: (s := _status(c, body["preview_id"]))["state"] == "complete" and s, timeout=60)
            with anon() as a:
                a.get(st["vtt_url"])
                a.get(_tile_url(st["vtt_url"], 0))
            c.get("/v1/previews/" + "f" * 32)
        cap = st["vtt_url"].split("/v1/c/")[1].split("/")[0]
        srv.stop()
    finally:
        setup_logging("INFO")
    text = buf.getvalue()
    assert "GET /v1/c/[cap]/" in text, text[-3000:]          # access log present but redacted
    for secret in (marker, token, cap, "sig="):
        assert secret not in text, secret
    blob = b"".join(p.read_bytes() for p in srv.settings.data_dir.rglob("*") if p.is_file())
    assert marker.encode() not in blob and token.encode() not in blob
    logging.getLogger().setLevel(logging.INFO)


def test_no_outbound_on_idle(tmp_path):
    from openseek.app import Core
    from openseek.config import Settings
    core = Core(Settings().with_(data_dir=tmp_path / "idle")).start()
    try:
        time.sleep(1.5)
        conns = [c for c in psutil.Process().net_connections(kind="inet")
                 if c.raddr and c.raddr.ip not in ("127.0.0.1", "::1")]
        assert conns == []
    finally:
        core.stop()


def test_bounded_two_workers_parallel_extraction(origin, live_factory):
    srv = live_factory(_policy(origin), pregen="full", workers_per_source=2)
    token, _ = _register(srv)
    with srv.client(token) as c:
        body = _create(c, _url(origin, "main.mkv"), at_ms=20_000, layout="tiles")
        st = wait_for(lambda: (s := _status(c, body["preview_id"]))["state"] == "complete" and s, timeout=60)
        tiles = _tiles_in_vtt(c, st["vtt_url"])
        assert len(tiles) == 12
        with anon() as a:
            for k, t in tiles.items():
                frame = decode_barcode(a.get(_tile_url(st["vtt_url"], k)).content)
                assert frame is not None and abs(frame * 1000 / FPS - k * 10_000) <= 5_000
                assert abs(frame * 1000 / FPS - t) <= 1000 / FPS + 1


def test_grab_batch_and_stateful_forward_decode(origin, media):
    from openseek.engine import Extractor, ProxyReader
    from openseek.fetch import Budget, ProxySource, RangeProxy, Upstream

    proxy = RangeProxy().start()
    up = Upstream(_policy(origin), _url(origin, "gop11.mkv"), {}, Budget(1 << 30))
    try:
        pre = up.preflight()
        cap = proxy.register(ProxySource(up, pre.size, [(0, pre.head), (pre.tail_offset, pre.tail)]))
        rd = ProxyReader(proxy.url(cap), pre.size)
        ex = Extractor(rd, pre.fmt)
        try:
            batch = ex.grab_batch([0, 10_000, 20_000, 30_000], tolerance_ms=5000)
            assert len(batch) == 4
            for t_ms, pts_ms, acc, jpg in batch:
                assert abs(pts_ms - t_ms) <= 5000
                frame = decode_barcode(jpg)
                assert frame is not None and abs(frame * 1000 / FPS - pts_ms) <= 1000 / FPS + 1
            assert any(acc for _, _, acc, _ in batch)
        finally:
            ex.close()
    finally:
        proxy.stop()
        up.close()

