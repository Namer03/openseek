"""Cross-release thumbnail reuse (OpenSeek 2.0, stage 1). See docs/ARCHITECTURE.md.

A *library* match is never a proof: ids plus duration say "probably the same title", not "the same timeline". Every
result is therefore flagged approximate (verified is reserved for content-verified mappings, a later stage) and carries
a mapping t_cache = a * t_guest + b that the device can adjust or reject. Lookup is a pure read of the cache: it never
creates a job or a source and never touches the network.

Stored only with the owner's explicit opt-in (settings.metadata_index): IMDb id, TMDB id, season, episode and the
server-observed file size, next to the cache key. Never titles, file names, URLs, headers or client claims about size/hash.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from .db import DB
from .ids import new_preview_id

IMDB = re.compile(r"tt\d{1,10}")
MAX_TMDB = 10 ** 9
MAX_NUM = 9999
SAME_LENGTH_MS = 1000
SPEED_RATIOS = (25 / 24, 24 / 25)
SPEED_TOL = 0.0015
LENGTH_DIFFERS_MIN_MS = 120_000
LENGTH_DIFFERS_FRAC = 0.02
MAX_OFFSET_MS = 6 * 3600 * 1000
MAX_BORROWED_PER_DEVICE = 300
MAX_CANDIDATES = 3
KIND_RANK = {"same_length": 0, "speed_adjusted": 1, "length_differs": 2}

REASONS = {
    "same_length": "ids and duration match; the timelines may still differ by an intro or an edit",
    "speed_adjusted": "ids match and the duration ratio looks like a 24/25 fps conversion; scaled to fit",
    "length_differs": "ids match but the durations differ; an offset is probably needed",
}


@dataclass(frozen=True)
class Ids:
    imdb: str | None
    tmdb: int | None
    season: int
    episode: int


def clean_ids(imdb, tmdb, season, episode) -> Ids | None:
    """Strictly validated ids, or None when there is nothing usable (then nothing is stored or matched)."""
    im = imdb if isinstance(imdb, str) and IMDB.fullmatch(imdb) else None
    tm = tmdb if isinstance(tmdb, int) and not isinstance(tmdb, bool) and 0 < tmdb < MAX_TMDB else None
    if im is None and tm is None:
        return None
    s = season if isinstance(season, int) and not isinstance(season, bool) and 0 <= season <= MAX_NUM else 0
    e = episode if isinstance(episode, int) and not isinstance(episode, bool) and 0 <= episode <= MAX_NUM else 0
    return Ids(im, tm, s, e)


def release_key(ids: Ids, duration_ms: int, size: int | None) -> str:
    """Identity of the GUEST's release as the guest describes it; keys that device's offsets and rejections."""
    raw = f"{ids.imdb or ''}|{ids.tmdb or ''}|{ids.season}|{ids.episode}|{round(duration_ms / 1000)}|{size or 0}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def relation(guest_ms: int, cache_ms: int) -> tuple[str, float, float] | None:
    """(kind, a, b) with t_cache = a * t_guest + b, or None when the durations are too far apart to offer anything.
    Same file size never matters here: it does not make two encodes the same release."""
    if guest_ms <= 0 or cache_ms <= 0:
        return None
    d = abs(guest_ms - cache_ms)
    if d <= SAME_LENGTH_MS:
        return "same_length", 1.0, 0.0
    ratio = cache_ms / guest_ms
    if any(abs(ratio - r) <= SPEED_TOL * r for r in SPEED_RATIOS):
        return "speed_adjusted", ratio, 0.0
    if d <= max(LENGTH_DIFFERS_MIN_MS, LENGTH_DIFFERS_FRAC * max(guest_ms, cache_ms)):
        return "length_differs", 1.0, 0.0
    return None


def to_guest_time(t_cache_ms: float, a: float, b: float) -> float:
    return (t_cache_ms - b) / a


class Library:
    def __init__(self, db: DB, cache, clock=time.time):
        self.db, self.cache, self.clock = db, cache, clock

    # ------------------------------------------------------------------ owner opt-in
    def enabled(self) -> bool:
        return self.db.get_setting("metadata_index") == "1"

    def set_enabled(self, on: bool, purge: bool = False) -> None:
        self.db.set_setting("metadata_index", "1" if on else "0")
        if purge:
            self.db.purge_release_meta()

    def record(self, fp: str, hints: dict | None, size_bytes: int, replace: bool = True) -> bool:
        """Remember which title/episode this cache entry is, from the client's own hints. Off => nothing is stored."""
        if not self.enabled() or not hints or not fp:
            return False
        ids = clean_ids(hints.get("imdb_id"), hints.get("tmdb_id"), hints.get("season"), hints.get("episode"))
        if ids is None:
            return False
        self.db.upsert_release_meta(fp, ids.imdb, ids.tmdb, ids.season, ids.episode, int(size_bytes or 0), replace)
        return True

    # ------------------------------------------------------------------ lookup (a pure read of the cache)
    def lookup(self, device_id: str, ids: Ids, duration_ms: int, size_bytes: int | None) -> dict:
        key = release_key(ids, duration_ms, size_bytes)
        rejected = self.db.rejected_sources(device_id, key)
        found = []
        for fp, c_size, c_dur, n_tiles, tiles in self.db.release_candidates(ids.imdb, ids.tmdb, ids.season, ids.episode):
            if fp in rejected or tiles <= 0:
                continue
            rel = relation(duration_ms, c_dur)
            if rel is None:
                continue
            kind, a, b = rel
            found.append((KIND_RANK[kind], -(tiles / max(1, n_tiles or 1)), abs(duration_ms - c_dur), fp, kind, a, b,
                          c_dur, n_tiles, tiles))
        found.sort()
        out = []
        for _r, _c, _d, fp, kind, a, b, c_dur, n_tiles, tiles in found[:MAX_CANDIDATES]:
            pid = self.db.borrowed_for(device_id, key, fp)
            if pid is None:                                    # the DEFAULT mapping is stored; an offset overrides it
                pid = new_preview_id()
                self.db.add_borrowed(device_id, key, fp, pid, a, b, kind, int(duration_ms), MAX_BORROWED_PER_DEVICE)
            else:
                self.db.update_borrowed_mapping(pid, a, b, kind)
            show_a, show_b, show_kind = a, b, kind
            off = self.db.get_offset(device_id, key, fp)
            if off is not None:
                show_a, show_b, show_kind = off[0], off[1], "user_adjusted"
            out.append({"preview_id": pid, "kind": show_kind, "approximate": True, "verified": False,
                        "reason": REASONS.get(kind, "adjusted by this device"), "mapping": {"a": show_a, "b": show_b},
                        "source": {"duration_ms": c_dur, "tiles": tiles, "n_tiles": n_tiles,
                                   "coverage": round(tiles / max(1, n_tiles or 1), 3)}})
        return {"release_key": key, "candidates": out}

    # ------------------------------------------------------------------ what a borrowed preview shows
    def mapping_for(self, preview_id: str) -> tuple[float, float, int, str] | None:
        """(a, b, guest_duration_ms, kind) for a borrowed preview: this device's own offset wins over the default."""
        row = self.db.borrowed(preview_id)
        if row is None:
            return None
        device_id, key, src_fp, a, b, kind, gdur = row
        off = self.db.get_offset(device_id, key, src_fp)
        if off is not None:
            return off[0], off[1], gdur, "user_adjusted"
        return a, b, gdur, kind

    def set_offset(self, device_id: str, preview_id: str, offset_ms: int) -> dict | None:
        row = self.db.borrowed(preview_id)
        if row is None or row[0] != device_id or abs(offset_ms) > MAX_OFFSET_MS:
            return None
        _d, key, src_fp, a, _b, _kind, _gdur = row               # a = the default scale (1 or the 24/25 factor)
        self.db.put_offset(device_id, key, src_fp, a, float(offset_ms))
        return {"mapping": {"a": a, "b": float(offset_ms)}, "kind": "user_adjusted", "approximate": True}

    def clear_offset(self, device_id: str, preview_id: str) -> bool:
        row = self.db.borrowed(preview_id)
        if row is None or row[0] != device_id:
            return False
        self.db.delete_offset(device_id, row[1], row[2])
        return True

    def reject(self, device_id: str, preview_id: str) -> bool:
        """This device does not want this source for this release any more. Touches nothing shared."""
        row = self.db.borrowed(preview_id)
        if row is None or row[0] != device_id:
            return False
        self.db.reject_borrowed(device_id, row[1], row[2], preview_id)
        return True
