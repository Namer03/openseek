"""SR-15/SR-16: atomic tiles, corruption detection, LRU eviction + retention, path hygiene."""
from __future__ import annotations

import io
import os
import time

import pytest
from PIL import Image

from openseek.cache import PROFILE, Cache, atomic_write, jpeg_ok
from openseek.db import DB

FP1, FP2, FP3 = "1" * 64, "2" * 64, "3" * 64


def jpeg(color=(200, 10, 10)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(b, "JPEG", quality=75)
    return b.getvalue()


@pytest.fixture()
def cache(tmp_path):
    db = DB(tmp_path / "t.db")
    t = [1_000_000.0]
    c = Cache(tmp_path, db, max_bytes=10 << 20, retention_days=30, clock=lambda: t[0])
    c.t = t
    yield c
    db.close()


def test_put_and_read_tile_atomic_layout(cache, tmp_path):
    data = jpeg()
    v = cache.put_tile(FP1, 7, 70_040, data)
    assert v == 1 and cache.tile_bytes(FP1, 7) == data
    p = tmp_path / "cache" / FP1[:2] / FP1 / PROFILE / "tiles" / "7.jpg"
    assert p.read_bytes() == data
    assert not list(p.parent.glob(".*.tmp"))
    assert cache.put_tile(FP1, 7, 1, jpeg((0, 0, 0))) == 1          # immutable once published
    assert cache.tile_bytes(FP1, 7) == data
    if os.name == "posix":
        assert (p.stat().st_mode & 0o777) == 0o600
        assert (p.parent.stat().st_mode & 0o777) == 0o700


def test_rejects_non_jpeg_and_bad_ids(cache):
    with pytest.raises(ValueError):
        cache.put_tile(FP1, 1, 0, b"\x89PNG....")
    for bad in ("../" + "a" * 61, "A" * 64, "a" * 63, "a" * 65, "..", "%2e%2e" + "a" * 58):
        with pytest.raises(ValueError):
            cache.put_tile(bad, 1, 0, jpeg())
        assert cache.tile_bytes(bad, 1) is None
    with pytest.raises(ValueError):
        cache.put_tile(FP1, -1, 0, jpeg())


def test_corrupt_tile_detected_dropped_and_reported(cache, tmp_path):
    lost = []
    cache.on_tile_lost = lambda fp, k: lost.append((fp, k))
    cache.put_tile(FP1, 3, 30_000, jpeg())
    p = tmp_path / "cache" / FP1[:2] / FP1 / PROFILE / "tiles" / "3.jpg"
    p.write_bytes(p.read_bytes()[:500])                 # truncated (no EOI)
    v0 = cache.entry(FP1).version
    assert cache.tile_bytes(FP1, 3) is None
    assert lost == [(FP1, 3)] and 3 not in cache.entry(FP1).tiles and cache.entry(FP1).version == v0 + 1
    assert not p.exists()
    cache.put_tile(FP1, 3, 30_000, jpeg())              # regenerated
    assert jpeg_ok(cache.tile_bytes(FP1, 3))


def test_same_size_garbage_detected(cache, tmp_path):
    good = jpeg()
    cache.put_tile(FP1, 4, 40_000, good)
    p = tmp_path / "cache" / FP1[:2] / FP1 / PROFILE / "tiles" / "4.jpg"
    p.write_bytes(b"\x00" * len(good))
    assert cache.tile_bytes(FP1, 4) is None


def test_sheet_composed_from_tiles(cache):
    for k in (0, 1, 99):
        cache.put_tile(FP1, k, k * 10_000, jpeg((k, 255 - k, 0)))
    data, v = cache.sheet_bytes(FP1, 0)
    assert v == 3
    im = Image.open(io.BytesIO(data))
    assert im.size == (3200, 1800)
    r, g, _ = im.getpixel((160, 90))
    assert g > 200 and r < 40                           # tile 0 in slot 0
    assert im.getpixel((1600, 900)) == (0, 0, 0)        # unfilled slot is black
    assert cache.sheet_bytes(FP1, 0)[0] == data         # cached
    cache.put_tile(FP1, 50, 500_000, jpeg())
    assert cache.sheet_bytes(FP1, 0)[1] == 4            # new fill version
    assert cache.sheet_bytes(FP1, 1) is None


def test_persistence_across_instances(tmp_path):
    db = DB(tmp_path / "t.db")
    c = Cache(tmp_path, db, 10 << 20, 30)
    c.set_media(FP1, 120_000, 12)
    c.put_tile(FP1, 2, 19_960, jpeg())
    db.close()
    db2 = DB(tmp_path / "t.db")
    c2 = Cache(tmp_path, db2, 10 << 20, 30)
    e = c2.entry(FP1)
    assert e.duration_ms == 120_000 and e.n_tiles == 12 and e.tiles == {2: 19_960} and e.version == 1
    assert jpeg_ok(c2.tile_bytes(FP1, 2))
    db2.close()


def test_lru_eviction_by_bytes(tmp_path):
    db = DB(tmp_path / "t.db")
    t = [1000.0]
    c = Cache(tmp_path, db, max_bytes=16 << 20, retention_days=30, clock=lambda: t[0])
    big = jpeg()
    for fp in (FP1, FP2, FP3):
        for k in range(10):
            c.put_tile(fp, k, k, big)
        t[0] += 10
    c.touch(FP1)                                        # FP1 becomes most recently used
    per = c.entry(FP2).bytes
    c.max_bytes = per * 2 + 1
    gone = c.evict()
    assert gone == [FP2]
    assert c.entry(FP2) is None and not (tmp_path / "cache" / FP2[:2] / FP2).exists()
    assert c.entry(FP1) is not None and c.entry(FP3) is not None
    c.max_bytes = 1
    assert c.evict(protected={FP3}) == [FP1]            # active sources are protected
    db.close()


def test_retention(tmp_path):
    db = DB(tmp_path / "t.db")
    t = [1000.0]
    c = Cache(tmp_path, db, max_bytes=1 << 30, retention_days=1, clock=lambda: t[0])
    c.put_tile(FP1, 0, 0, jpeg())
    t[0] += 86400 + 1
    c.put_tile(FP2, 0, 0, jpeg())
    assert c.evict() == [FP1]
    db.close()


def test_atomic_write_leaves_no_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "x.jpg"
    atomic_write(target, b"old")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.tmp"))
