"""Container sniffing and the cheap "does this Matroska file have an index?" check.

Gate 1 measured that seeking in an MKV without Cues degenerates into a linear scan (84 % of the
file for one frame). We therefore refuse such files (`no_index`) *before* the decoder opens them,
using only bytes we already hold (the 256 KiB head and the 64 KiB tail of the preflight) plus at
most two tiny ranged reads at the positions the SeekHead points to. Nothing here scans clusters.
"""
from __future__ import annotations

from typing import Callable

EBML_ID = 0x1A45DFA3
DOCTYPE_ID = 0x4282
SEGMENT_ID = 0x18538067
SEEKHEAD_ID = 0x114D9B74
SEEK_ID = 0x4DBB
SEEKID_ID = 0x53AB
SEEKPOS_ID = 0x53AC
CUES_ID = 0x1C53BB6B
CLUSTER_ID = 0x1F43B675
VOID_ID = 0xEC

MP4_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot"}


class Truncated(Exception):
    pass


def _vint(buf: bytes, i: int, keep_marker: bool) -> tuple[int, int]:
    if i >= len(buf):
        raise Truncated
    b0 = buf[i]
    length, mask = 1, 0x80
    while length <= 8 and not (b0 & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("bad vint")
    if i + length > len(buf):
        raise Truncated
    val = b0 if keep_marker else (b0 & (mask - 1))
    for c in buf[i + 1:i + length]:
        val = (val << 8) | c
    return val, i + length


def _element(buf: bytes, i: int) -> tuple[int, int, int, bool]:
    """(id, data_start, size, unknown_size) for the element header at i."""
    eid, j = _vint(buf, i, True)
    size, k = _vint(buf, j, False)
    unknown = size == (1 << (7 * (k - j))) - 1
    return eid, k, size, unknown


def sniff(head: bytes) -> str | None:
    """'matroska' | 'mp4' | None (anything else, including HLS/DASH manifests and HTML)."""
    if len(head) >= 4 and int.from_bytes(head[:4], "big") == EBML_ID:
        try:
            eid, ds, size, _ = _element(head, 0)
            end = min(len(head), ds + size)
            i = ds
            while i < end:
                cid, cds, csize, _ = _element(head, i)
                if cid == DOCTYPE_ID:
                    doctype = head[cds:cds + csize].rstrip(b"\x00")
                    return "matroska" if doctype in (b"matroska", b"webm") else None
                i = cds + csize
        except (Truncated, ValueError):
            return None
        return None
    if len(head) >= 8 and head[4:8] in MP4_BOXES:
        size = int.from_bytes(head[:4], "big")
        if size == 1 or size >= 8:
            return "mp4"
    return None


def _seek_entries(buf: bytes, ds: int, size: int) -> list[tuple[int, int]]:
    out = []
    i, end = ds, min(len(buf), ds + size)
    while i < end:
        eid, cds, csize, _ = _element(buf, i)
        if eid == SEEK_ID:
            sid, spos = None, None
            j, jend = cds, cds + csize
            while j < jend:
                fid, fds, fsize, _ = _element(buf, j)
                data = buf[fds:fds + fsize]
                if fid == SEEKID_ID:
                    sid = int.from_bytes(data, "big")
                elif fid == SEEKPOS_ID:
                    spos = int.from_bytes(data, "big")
                j = fds + fsize
            if sid is not None and spos is not None:
                out.append((sid, spos))
        i = cds + csize
    return out


def mkv_has_index(head: bytes, size: int, read_at: Callable[[int, int], bytes]) -> bool:
    """True if the Segment's SeekHead (or a top-level element before the first Cluster)
    leads to a real Cues element. `read_at(offset, length)` performs a small ranged read."""
    try:
        eid, ds, esize, _ = _element(head, 0)
        if eid != EBML_ID:
            return False
        i = ds + esize
        sid, seg_start, seg_size, _ = _element(head, i)
        if sid != SEGMENT_ID:
            return False
    except (Truncated, ValueError):
        return False

    targets: list[tuple[int, int]] = []
    i = seg_start
    try:
        while i < len(head):
            cid, cds, csize, unknown = _element(head, i)
            if cid == CUES_ID and csize > 0:
                return True
            if cid == CLUSTER_ID or unknown:
                break
            if cid == SEEKHEAD_ID:
                if cds + csize > len(head):
                    break
                targets += _seek_entries(head, cds, csize)
            i = cds + csize
    except (Truncated, ValueError):
        pass

    def element_at(pos: int, want: int, length: int) -> bytes | None:
        absolute = seg_start + pos
        if absolute >= size:
            return None
        data = read_at(absolute, min(length, size - absolute))
        try:
            eid2, d2, s2, _ = _element(data, 0)
        except (Truncated, ValueError):
            return None
        if eid2 != want:
            return None
        return data if want != CUES_ID or s2 > 0 else None

    for tid, pos in targets:
        if tid == CUES_ID and element_at(pos, CUES_ID, 16) is not None:
            return True
    # mkvmerge may chain a second SeekHead (at the end of the file) that lists the Cues
    for tid, pos in targets:
        if tid != SEEKHEAD_ID:
            continue
        data = element_at(pos, SEEKHEAD_ID, 4096)
        if data is None:
            continue
        try:
            _, d2, s2, _ = _element(data, 0)
            for tid2, pos2 in _seek_entries(data, d2, s2):
                if tid2 == CUES_ID and element_at(pos2, CUES_ID, 16) is not None:
                    return True
        except (Truncated, ValueError):
            continue
    return False
