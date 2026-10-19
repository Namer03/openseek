"""Choosing a lower-resolution copy for thumbnails (pure logic; no network, nothing is resolved here).

The client reports candidate METADATA only. `rank()` returns the eligible candidates best-first so the
caller can resolve ONE link, preflight it, validate its timeline, and only then try the next (bounded).
The playback source is never replaced; this module cannot touch it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words that mean a DIFFERENT picture or cut. "hybrid" (a Dolby Vision source mix) and "dubbed" (audio only) are
# deliberately absent: the frames and the timeline are the same. 3D and open-matte change the picture itself.
EDITION_WORDS = ("extended", "directors", "director", "theatrical", "unrated", "imax", "remastered", "final cut",
                 "special edition", "uncut", "redux", "open matte", "3d", "bd3d", "sbs", "hsbs")
OK_CODECS = ("h264", "avc", "x264", "hevc", "h265", "x265")
OK_CONTAINERS = ("mkv", "matroska", "mp4", "m4v")
MAX_THUMB_HEIGHT = 1080
MIN_THUMB_HEIGHT = 480
DURATION_TOL = 0.10          # same gate as timeline.metadata_gate
MAX_ATTEMPTS = 2             # resolutions we are willing to spend per playback request


@dataclass(frozen=True)
class Candidate:
    cid: str                 # opaque id the client can map back to its stream entry
    title_key: str           # imdb id (+ :season:episode); equality decides "same title"
    height: int
    duration_ms: int
    size: int
    codec: str = ""
    container: str = ""
    name: str = ""           # release name; only used to read edition words
    cached: bool = False     # debrid reports it instantly available (no download to start)
    resolved: bool = False   # the client already holds a usable link
    editions: frozenset = field(default_factory=frozenset)

    @staticmethod
    def edition_set(name: str) -> frozenset:
        n = re.sub(r"[._\-]+", " ", (name or "").lower())
        return frozenset(w for w in EDITION_WORDS if re.search(rf"\b{re.escape(w)}\b", n))

    @property
    def bitrate_bps(self) -> float:
        return self.size * 8 / (self.duration_ms / 1000) if self.duration_ms > 0 else 0.0

    def edition_words(self) -> frozenset:
        return self.editions or Candidate.edition_set(self.name)


def _has(value: str, allowed: tuple) -> bool:
    v = (value or "").lower()
    return any(a in v for a in allowed)


def reject_reason(play: Candidate, c: Candidate) -> str | None:
    """Why `c` may not be used as a thumbnail source for `play` (None = eligible)."""
    if c.cid == play.cid:
        return "same_source"
    if c.title_key != play.title_key:
        return "other_title"
    if not c.cached:
        return "not_cached"                       # never start a download / touch the account
    if not (MIN_THUMB_HEIGHT <= c.height <= MAX_THUMB_HEIGHT) or c.height >= play.height:
        return "resolution"
    if play.duration_ms > 0 and c.duration_ms > 0 and abs(c.duration_ms / play.duration_ms - 1) > DURATION_TOL:
        return "duration"
    if play.edition_words() != c.edition_words():
        return "edition"
    if c.codec and not _has(c.codec, OK_CODECS):
        return "codec"
    if c.container and not _has(c.container, OK_CONTAINERS):
        return "container"
    if c.size <= 0:
        return "size"
    return None


def rank(play: Candidate, cands: list[Candidate]) -> list[Candidate]:
    """Eligible candidates, best first: link already held, then closest to 720p, then lowest bitrate."""
    ok = [c for c in cands if reject_reason(play, c) is None]
    # runtime is often unknown to the client (bitrate 0), so size is the cost proxy that breaks the tie
    return sorted(ok, key=lambda c: (not c.resolved, abs(c.height - 720), c.bitrate_bps, c.size, c.cid))


def explain(play: Candidate, cands: list[Candidate], with_names: bool = False) -> list[dict]:
    """Metadata-only view for diagnostics (no URLs): why each candidate is or is not eligible and, in a ranked order,
    which one would be asked for. Release names only on request (dry-run diagnostics), never persisted."""
    out = []
    for c in cands:
        why = reject_reason(play, c)
        out.append({"cid": c.cid, "height": c.height, "size_mb": round(c.size / 1e6), "cached": c.cached,
                    "resolved": c.resolved, "bitrate_mbps": round(c.bitrate_bps / 1e6, 1),
                    "editions": sorted(c.edition_words()), "eligible": why is None, "reject": why,
                    "rank": None, **({"name": c.name[:120]} if with_names else {})})
    order = [c.cid for c in rank(play, cands)]
    for row in out:
        row["rank"] = order.index(row["cid"]) + 1 if row["cid"] in order else None
    return out


def plan_attempts(play: Candidate, cands: list[Candidate]) -> list[Candidate]:
    return rank(play, cands)[:MAX_ATTEMPTS]
