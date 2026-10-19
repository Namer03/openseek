from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import LiveServer, make_policy, make_video, remux, void_out_cues  # noqa: E402
from origin import Canary, Origin  # noqa: E402


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> Path:
    """Small fixtures generated once per session (a few MB, ~10 s of ffmpeg)."""
    d = tmp_path_factory.mktemp("media")
    make_video(d / "main.mkv", 120)                              # 640x360, GOP 2 s, Cues at end
    remux(d / "main.mkv", d / "main.mp4")                        # moov at end
    remux(d / "main.mkv", d / "nocues.mkv")
    assert void_out_cues(d / "nocues.mkv") == 1
    make_video(d / "gop11.mkv", 60, gop=264)                     # 11 s GOP -> accurate re-extract
    make_video(d / "pillar.mkv", 12, w=480, h=360)               # 4:3 -> pillarbox
    make_video(d / "hdr.mkv", 6, kbps=300, gop=24, hevc_pq=True)  # HEVC Main10 PQ
    (d / "random.bin").write_bytes(os.urandom(1 << 20))
    (d / "page.html").write_bytes(b"<!doctype html><title>x</title>" + b"x" * 300000)
    return d


@pytest.fixture()
def origin(media):
    o = Origin(media).start()
    yield o
    o.stop()


@pytest.fixture()
def canary():
    c = Canary()
    yield c
    c.stop()


@pytest.fixture()
def live_factory(tmp_path):
    made = []

    def make(policy, data_dir: Path | None = None, **overrides) -> LiveServer:
        srv = LiveServer(data_dir or (tmp_path / f"data{len(made)}"), policy, **overrides)
        made.append(srv)
        return srv

    yield make
    for s in made:
        try:
            s.stop()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["make_policy"]
