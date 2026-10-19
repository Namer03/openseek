from __future__ import annotations

from helpers import parse_vtt
from openseek.vtt import complete_sheets, cues, render, ts

BASE = "https://192.168.1.20:8787"
PID = "ab" * 16
CAP = "C" * 32


def test_timestamp_format_hours_not_minutes_over_60():
    assert ts(4_797_960) == "01:19:57.960"          # never "00:79:57"
    assert ts(0) == "00:00:00.000" and ts(36_000_000) == "10:00:00.000"


def test_cue_spans_tolerance_and_clips_at_midpoints():
    c = cues({0: 0, 1: 9_000, 2: 20_000, 10: 98_000}, 100_000, 5_000)
    assert [(x.start_ms, x.end_ms, x.k) for x in c] == [
        (0, 4_500, 0),            # clipped at 0 and at midpoint(0, 9000)
        (4_500, 14_000, 1),       # [9000-4500 midpoint, 9000+5000)
        (15_000, 25_000, 2),      # full ±5 s: neighbours far enough
        (93_000, 100_000, 10),    # end clipped at duration
    ]
    for a, b in zip(c, c[1:]):
        assert a.end_ms <= b.start_ms


def test_gaps_have_no_preview_and_coarse_tiles_are_not_stretched():
    c = cues({0: 0, 6: 60_000}, 120_000, 5_000)
    assert [(x.start_ms, x.end_ms) for x in c] == [(0, 5_000), (55_000, 65_000)]


def test_duplicate_pts_keep_lowest_k():
    c = cues({3: 25_000, 2: 25_000}, 60_000, 5_000)
    assert len(c) == 1 and c[0].k == 2


def test_render_tiles_layout_urls():
    text = render(BASE, CAP, PID, 1_800_000_000, "tiles", {480: 4_797_960}, 7_234_000, 724, 5_000)
    parsed = parse_vtt(text)
    assert parsed == [(4_792_960, 4_802_960,
                       f"{BASE}/v1/c/{CAP}/{PID}/tiles/480.jpg?exp=1800000000&t=4797960#xywh=0,0,320,180")]
    assert "01:19:52.960 --> 01:20:02.960" in text


def test_render_sheets_layout_carries_per_cue_t_and_fill_version():
    tiles = {101: 1_010_000, 199: 1_990_000}
    parsed = parse_vtt(render(BASE, CAP, PID, 1, "sheets", tiles, 3_000_000, 300, 5_000))
    urls = [u for _, _, u in parsed]
    assert urls[0] == f"{BASE}/v1/c/{CAP}/{PID}/sheets/1.jpg?exp=1&v=2&t=1010000#xywh=320,0,320,180"
    assert urls[1] == f"{BASE}/v1/c/{CAP}/{PID}/sheets/1.jpg?exp=1&v=2&t=1990000#xywh=2880,1620,320,180"
    assert urls[0].split("&t=")[0] == urls[1].split("&t=")[0]   # same sheet once t is stripped


def test_render_hybrid_uses_sheets_only_for_complete_blocks():
    n_tiles = 150
    tiles = {k: k * 10_000 for k in range(100)}       # sheet 0 complete
    tiles[120] = 1_200_000                            # sheet 1 partial
    assert complete_sheets(tiles, n_tiles) == {0}
    urls = [u for _, _, u in parse_vtt(render(BASE, CAP, PID, 1, "hybrid", tiles, 1_500_000, n_tiles, 5_000))]
    assert all("/sheets/0.jpg" in u for u in urls[:100])
    assert "/tiles/120.jpg" in urls[100]
    # a short last sheet is complete when all of its existing tiles are there
    assert complete_sheets({k: 0 for k in range(100, 150)} | {k: 0 for k in range(100)}, 150) == {0, 1}


def test_empty_vtt_is_valid():
    assert render(BASE, CAP, PID, 1, "hybrid", {}, None, None, 5_000) == "WEBVTT\n"
    assert parse_vtt("WEBVTT\n") == []
