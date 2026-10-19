"""WebVTT generation (CONTRACT-v1 "VTT semantics", SCRUBBING.md §3).

* Only published tiles appear. A tile with real pts p gets the cue [p - tol, p + tol), clipped
  at the midpoint between neighbouring published tiles' pts and at [0, duration].
* Cues are sorted and non-overlapping; positions outside every cue have no preview.
* Every URL is absolute (scheme/host/port the client used) and carries `&t=<pts_ms>` before
  the `#xywh` fragment. Layouts: tiles | sheets | hybrid (complete 10x10 blocks as sheets).
"""
from __future__ import annotations

from dataclasses import dataclass

from .cache import PER_SHEET, SHEET_COLS, TILE_H, TILE_W

LAYOUTS = ("tiles", "sheets", "hybrid")


def ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:03d}"


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    k: int
    pts_ms: int


def cues(tiles: dict[int, int], duration_ms: int | None, tolerance_ms: int) -> list[Cue]:
    """tiles: k -> pts_ms. Tiles sharing a pts keep the lowest k."""
    by_pts: dict[int, int] = {}
    for k, p in tiles.items():
        if p not in by_pts or k < by_pts[p]:
            by_pts[p] = k
    pts = sorted(by_pts)
    end_limit = duration_ms if duration_ms and duration_ms > 0 else None
    out = []
    for i, p in enumerate(pts):
        start = p - tolerance_ms
        end = p + tolerance_ms
        if i > 0:
            start = max(start, (pts[i - 1] + p) // 2)
        if i + 1 < len(pts):
            end = min(end, (p + pts[i + 1]) // 2)
        start = max(0, start)
        if end_limit is not None:
            end = min(end, end_limit)
        if end > start:
            out.append(Cue(start, end, by_pts[p], p))
    return out


def complete_sheets(tiles: dict[int, int], n_tiles: int | None) -> set[int]:
    if not n_tiles:
        return set()
    out = set()
    for n in range((n_tiles - 1) // PER_SHEET + 1):
        lo, hi = n * PER_SHEET, min(n_tiles, (n + 1) * PER_SHEET)
        if all(k in tiles for k in range(lo, hi)):
            out.add(n)
    return out


def render(base_url: str, cap: str, preview_id: str, exp: int, layout: str, tiles: dict[int, int],
           duration_ms: int | None, n_tiles: int | None, tolerance_ms: int,
           mapping: "tuple[float, float, int] | None" = None) -> str:
    """mapping = (a, b, guest_duration_ms) for a borrowed preview: cue times are cache time; the guest sees
    t_guest = (t_cache - b) / a, clipped to the guest's own duration. Images and `t=` (cache pts) are unchanged."""
    if layout not in LAYOUTS:
        raise ValueError("layout")
    prefix = f"{base_url}/v1/c/{cap}/{preview_id}"
    fill: dict[int, int] = {}
    for k in tiles:
        fill[k // PER_SHEET] = fill.get(k // PER_SHEET, 0) + 1
    full = complete_sheets(tiles, n_tiles) if layout == "hybrid" else set()
    lines = ["WEBVTT", ""]
    for c in cues(tiles, duration_ms, tolerance_ms):
        start_ms, end_ms = c.start_ms, c.end_ms
        if mapping is not None:
            a, b, gdur = mapping
            start_ms = max(0, int(round((c.start_ms - b) / a)))
            end_ms = min(int(gdur), int(round((c.end_ms - b) / a)))
            if end_ms <= start_ms:
                continue                                     # the cue falls outside the guest's timeline
        n = c.k // PER_SHEET
        if layout == "sheets" or (layout == "hybrid" and n in full):
            slot = c.k % PER_SHEET
            x, y = (slot % SHEET_COLS) * TILE_W, (slot // SHEET_COLS) * TILE_H
            url = f"{prefix}/sheets/{n}.jpg?exp={exp}&v={fill[n]}&t={c.pts_ms}#xywh={x},{y},{TILE_W},{TILE_H}"
        else:
            url = f"{prefix}/tiles/{c.k}.jpg?exp={exp}&t={c.pts_ms}#xywh=0,0,{TILE_W},{TILE_H}"
        lines += [f"{ts(start_ms)} --> {ts(end_ms)}", url, ""]
    return "\n".join(lines)
