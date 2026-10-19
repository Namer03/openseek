"""End-to-end: real uvicorn + TLS, real worker subprocess, real PyAV, local range origin.

The origin listens on 127.0.0.1, which the production policy forbids; tests reach it only
through `TestOnlyAllowance` built in Python (helpers.make_policy)."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

import psutil
import pytest

from helpers import FPS, decode_barcode, make_policy, parse_vtt, peer_pin, wait_for

HOST = "media.example.com"


def _policy(origin, **kw):
    return make_policy([origin], {HOST: ["127.0.0.1"]}, **kw)


def _url(origin, name: str) -> str:
    return f"http://{HOST}:{origin.port}/f/{name}"


def _create(c, url: str, **body):
    payload = {"source": {"url": url}, **body}
    r = c.post("/v1/previews", json=payload)
    assert r.status_code == 202, r.text
    return r.json()


def _status(c, pid: str, **params) -> dict:
    r = c.get(f"/v1/previews/{pid}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _tiles_in_vtt(c, vtt_url: str) -> dict[int, int]:
    """k -> t (pts ms) for every tile referenced by the VTT (layout tiles)."""
    r = c.get(vtt_url.replace("https://", "https://"), headers={"Authorization": ""})
    assert r.status_code == 200
    out = {}
    for _, _, url in parse_vtt(r.text):
        path = urlsplit(url).path
        if "/tiles/" in path:
            k = int(path.rsplit("/", 1)[1][:-4])
            t = int(url.split("&t=")[1].split("#")[0])
            out[k] = t
    return out


def test_job_first_tile_vtt_and_cap_tiles(origin, live_factory):
    srv = live_factory(_policy(origin))
    assert peer_pin("127.0.0.1", srv.port) == srv.core.spki_pin
    token, _ = srv.pair()
    with srv.client(token) as c:
        body = _create(c, _url(origin, "main.mkv"), at_ms=60_000, playhead_ms=0, layout="tiles")
        pid = body["preview_id"]
        assert body["tile_interval_ms"] == 10_000 and body["tolerance_ms"] == 5000
        assert body["vtt_url"].startswith(f"https://127.0.0.1:{srv.port}/v1/c/")
        st = wait_for(lambda: (s := _status(c, pid, wait_version=0, wait_ms=1000))["state"] in ("ready", "complete")
                      and s, timeout=30)
        assert st["source_duration_ms"] == pytest.approx(120_000, abs=100)
        assert st["progress"]["tiles_total"] == 12

        src = srv.core.jobs.sources[srv.core.jobs.get(srv.core.jobs.jobs[pid].device_id, pid).fp]
        assert src.events[0].k == 6 and src.events[0].cls == "P0"     # the requested tile comes first
        event = src.events[0]
        assert event.requests >= 0 and event.bytes >= 0
        assert 0 < event.keyframe_seconds <= event.seconds
        assert 0 <= event.accurate_seconds <= event.seconds
        assert event.keyframe_seconds + event.accurate_seconds <= event.seconds
        assert event.accurate == (event.accurate_seconds > 0)

        vtt_url = st["vtt_url"]
        tiles = _tiles_in_vtt(c, vtt_url)
        assert 6 in tiles
        cues = parse_vtt(c.get(vtt_url).text)
        for (a0, a1, _), (b0, _, _) in zip(cues, cues[1:]):
            assert a1 <= b0                                           # sorted, non-overlapping
        for a, b, url in cues:
            t = int(url.split("&t=")[1].split("#")[0])
            assert a <= t < b and b - a <= 10_000                     # cue spans [pts-5 s, pts+5 s) clipped
            assert url.startswith(f"https://127.0.0.1:{srv.port}/v1/c/")
            assert url.endswith("#xywh=0,0,320,180")

    # data plane without any Authorization header: capability URL only
    import httpx

    with httpx.Client(verify=False) as anon:
        for k, t in tiles.items():
            assert abs(t - k * 10_000) <= 5_000
            url = vtt_url.split("/thumbs.vtt")[0] + f"/tiles/{k}.jpg?exp=" + vtt_url.split("exp=")[1].split("&")[0]
            r = anon.get(url)
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
            assert r.headers["cache-control"].startswith("private")
            from PIL import Image
            import io
            assert Image.open(io.BytesIO(r.content)).size == (320, 180)
            frame = decode_barcode(r.content)
            assert frame is not None, f"tile {k} has no readable barcode"
            assert abs(frame * 1000 / FPS - t) <= 1000 / FPS + 1      # label t is the frame's real pts
            assert abs(frame * 1000 / FPS - k * 10_000) <= 5_000      # within tolerance of the grid point
