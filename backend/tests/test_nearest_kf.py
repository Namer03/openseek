"""Nearest-keyframe selection (feature flag): a keyframe just AFTER the slot beats decoding forward."""
from __future__ import annotations

from origin import Origin
from openseek.engine import Extractor, ProxyReader


def _extractor(media, nearest_kf: bool):
    o = Origin(media).start()
    f = media / "gop11.mkv"
    return o, Extractor(ProxyReader(o.url("/f/gop11.mkv"), f.stat().st_size), "matroska", nearest_kf=nearest_kf)


def test_forward_keyframe_used_when_backward_is_out_of_tolerance(media):
    o, ex = _extractor(media, True)
    try:
        ex.grab(1000, False)                      # warm: libav loads the Cues on the first seek
        for t in (17_000, 28_000, 39_000):         # previous kf 6 s earlier, next kf 5 s later
            pts, jpg = ex.grab(t, False)
            assert jpg and abs(pts - t) <= 5000 and pts > t
    finally:
        ex.close(); o.stop()


def test_flag_off_keeps_backward_keyframe(media):
    o, ex = _extractor(media, False)
    try:
        ex.grab(1000, False)
        pts, _ = ex.grab(17_000, False)
        assert pts < 17_000 and abs(pts - 17_000) > 5000   # old behaviour: caller falls back to accurate
    finally:
        ex.close(); o.stop()
