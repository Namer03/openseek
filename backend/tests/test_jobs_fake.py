"""JobManager + Source runner with a fake engine (no PyAV): ordering, latest-wins cancellation,
tolerance re-extraction, crash recovery, dedup, capacity. The origin and preflight are real."""
from __future__ import annotations

import io
import threading
import time

import pytest
from PIL import Image

from helpers import make_policy, wait_for
from openseek.cache import Cache
from openseek.config import Settings
from openseek.db import DB
from openseek.fetch import RangeProxy
from openseek.jobs import Busy, JobManager
from openseek.worker import GrabResult, OpenResult, WorkerError

HOST = "media.example.com"


def _jpeg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (320, 180), (9, 9, 9)).save(b, "JPEG")
    return b.getvalue()


JPEG = _jpeg()


class FakeEngine:
    """Keyframes every `gop_ms`; keyframe mode returns the keyframe at/before t."""

    def __init__(self, duration_ms=3_600_000, gop_ms=2_000, delay=0.01, crash_on=None, log=None):
        self.duration_ms, self.gop_ms, self.delay = duration_ms, gop_ms, delay
        self.crash_on = crash_on if crash_on is not None else {}
        self.opened = False
        self.log = log if log is not None else []
        self.kills = 0

    def is_open(self):
        return self.opened

    def open(self):
        self.opened = True
        return OpenResult(self.duration_ms, 640, 360, "h264", False)

    def grab(self, t_ms, accurate):
        time.sleep(self.delay)
        self.log.append((t_ms, accurate))
        if self.crash_on.get(t_ms, 0) > 0:
            self.crash_on[t_ms] -= 1
            self.opened = False
            raise WorkerError("crash", "eof", transient=True)
        pts = t_ms if accurate else (t_ms // self.gop_ms) * self.gop_ms
        return GrabResult(pts, JPEG)

    def kill(self):
        self.kills += 1
        self.opened = False

    def close(self):
        self.opened = False


@pytest.fixture()
def mgr_factory(origin, tmp_path):
    made = []

    def make(engine_kw=None, **settings_kw):
        db = DB(tmp_path / f"m{len(made)}.db")
        cache = Cache(tmp_path / f"d{len(made)}", db, 1 << 30, 30)
        proxy = RangeProxy().start()
        s = Settings().with_(**{"pregen": "full", "create_wait_s": 5.0, **settings_kw})
        engines = []

        def factory(src):
            e = FakeEngine(**(engine_kw or {}))
            engines.append(e)
            return e

        m = JobManager(s, make_policy([origin], {HOST: ["127.0.0.1"]}), cache, db, proxy, engine_factory=factory)
        m.engines = engines
        m.start()
        made.append((m, proxy, db))
        return m

    yield make
    for m, proxy, db in made:
        m.shutdown()
        proxy.stop()
        db.close()


def url(origin, name):
    return f"http://{HOST}:{origin.port}/f/{name}"


def src_of(m, job):
    return m.sources[job.fp]


def test_p0_first_then_latest_wins(origin, mgr_factory):
    m = mgr_factory({"delay": 0.03})
    job = m.create("d" * 16, url(origin, "main.mkv"), {}, 1_000_000, 0, "hybrid")
    src = src_of(m, job)
    wait_for(lambda: len(src.events) >= 3)
    n = len(src.events)
    m.priority(job, 3_000_000, 0)                     # user jumps far away
    wait_for(lambda: any(e.k == 300 for e in src.events))
    i = [e.k for e in src.events].index(300)
    assert i <= n + 1                                   # at most the in-flight frame finished first
    wait_for(lambda: len(src.events) >= i + 13)
    ks = [e.k for e in src.events]
    after = ks[i:i + 13]
    assert after[:5] == [300, 301, 299, 302, 298]
    assert all(e.cls == "P0" for e in src.events[i:i + 13])
    assert not any(94 <= k <= 106 for k in ks[i:i + 13])  # old P0 queue was dropped


def test_scrub_end_then_p1_then_p2(origin, mgr_factory):
    m = mgr_factory({"delay": 0.0}, pregen="overview")
    job = m.create("d" * 16, url(origin, "main.mkv"), {}, None, 600_000, "hybrid")
    src = src_of(m, job)
    wait_for(lambda: src.scheduler is not None and src.scheduler.complete_all is False and len(src.events) >= 37 + 5,
             timeout=20)
    cls = [e.cls for e in src.events]
    assert cls[:37] == ["P1"] * 37 and src.events[0].k == 60
    assert set(cls[37:]) == {"P2"}
    assert all(e.k % 6 == 0 for e in src.events[37:])


def test_keyframe_out_of_tolerance_reextracts_accurately(origin, mgr_factory):
    m = mgr_factory({"gop_ms": 12_000, "delay": 0.0}, pregen="off")
    job = m.create("d" * 16, url(origin, "main.mkv"), {}, 10_000, 0, "hybrid")
    src = src_of(m, job)
    wait_for(lambda: any(e.k == 1 for e in src.events))
    ev = next(e for e in src.events if e.k == 1)
    assert ev.accurate and ev.pts_ms == 10_000
    ev0 = next(e for e in src.events if e.k == 0)
    assert not ev0.accurate and ev0.pts_ms == 0
    tiles = m.cache.entry(job.fp).tiles
    assert all(abs(p - k * 10_000) <= 5_000 for k, p in tiles.items())


def test_worker_crash_recovery(origin, mgr_factory):
    m = mgr_factory({"crash_on": {50_000: 2}, "delay": 0.0}, pregen="off")
    job = m.create("d" * 16, url(origin, "main.mkv"), {}, 50_000, 0, "hybrid")
    src = src_of(m, job)
    wait_for(lambda: any(e.k == 5 for e in src.events), timeout=20)
    assert src.restarts >= 2 and src.error is None
    assert m.state(job)[0] in ("ready", "complete")


def test_persistent_crash_skips_tile_then_fails_source(origin, mgr_factory):
    m = mgr_factory({"crash_on": {k * 10_000: 99 for k in range(400)}, "delay": 0.0}, pregen="off")
    job = m.create("d" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")
    wait_for(lambda: m.state(job)[0] == "failed", timeout=60)
    assert m.state(job) == ("failed", "internal")


def test_dedup_same_device_same_content_and_shared_tiles(origin, mgr_factory):
    m = mgr_factory({"delay": 0.0}, pregen="off")
    origin.reset(redirects={"/r/main.mkv": "/f/main.mkv?sig=1"})
    a = m.create("d" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")
    b = m.create("d" * 16, f"http://{HOST}:{origin.port}/r/main.mkv", {}, 0, 0, "tiles")
    assert a.preview_id == b.preview_id                 # same device + same content -> same id
    c = m.create("e" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")
    assert c.preview_id != a.preview_id and c.fp == a.fp
    assert len(m.sources) == 1                          # tiles shared across devices by fingerprint


def test_global_cap_busy_and_per_device_supersede(origin, mgr_factory):
    m = mgr_factory({"delay": 0.0}, pregen="off", max_active_sources=1)
    a = m.create("a" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")
    wait_for(lambda: src_of(m, a).running)
    with pytest.raises(Busy):
        m.create("b" * 16, url(origin, "main.mp4"), {}, 0, 0, "hybrid")
    # the same device switching to another title stops its old worker instead of 429
    a2 = m.create("a" * 16, url(origin, "main.mp4"), {}, 0, 0, "hybrid")
    wait_for(lambda: not src_of(m, a).running and src_of(m, a2).running)
    assert len(m.running_sources()) == 1


def test_delete_stops_worker_keeps_tiles(origin, mgr_factory):
    m = mgr_factory({"delay": 0.05}, pregen="full")
    job = m.create("d" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")
    src = src_of(m, job)
    wait_for(lambda: len(src.events) >= 2)
    m.delete(job)
    assert not src.running
    n = len(m.cache.entry(job.fp).tiles)
    time.sleep(0.3)
    assert n >= 2 and len(m.cache.entry(job.fp).tiles) == n
    assert m.get("d" * 16, job.preview_id) is None


def test_idle_timeout_kills_worker(origin, mgr_factory):
    m = mgr_factory({"delay": 0.0}, pregen="off", idle_timeout_s=0.5)
    job = m.create("d" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")
    src = src_of(m, job)
    wait_for(lambda: src.running)
    wait_for(lambda: not src.running, timeout=10)
    assert src.error is None
    m.priority(job, 100_000, 0)                          # activity resumes the source
    wait_for(lambda: any(e.k == 10 for e in src.events))


def test_preflight_errors_become_job_errors(origin, mgr_factory, media):
    m = mgr_factory()
    origin.reset(range=False)
    assert m.state(m.create("d" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")) == ("failed", "range_unsupported")
    origin.reset()
    assert m.state(m.create("d" * 16, url(origin, "nocues.mkv"), {}, 0, 0, "hybrid")) == ("failed", "no_index")
    assert m.state(m.create("d" * 16, url(origin, "random.bin"), {}, 0, 0, "hybrid")) == ("failed", "not_video")
    assert m.state(m.create("d" * 16, url(origin, "nope.mkv"), {}, 0, 0, "hybrid")) == ("failed", "source_unreachable")
    origin.reset(expired=True)
    assert m.state(m.create("d" * 16, url(origin, "main.mkv"), {}, 0, 0, "hybrid")) == ("failed", "source_expired")
    assert not m.sources


def test_lost_tile_during_completed_decoder_close_is_regenerated(origin, mgr_factory):
    m = mgr_factory({"duration_ms": 10_000, "delay": 0})
    closing, release = threading.Event(), threading.Event()
    factory = m.engine_factory

    def delayed_close(src):
        engine = factory(src)
        original = engine.close
        if len(m.engines) == 1:
            def close():
                closing.set()
                release.wait(timeout=5)
                original()
            engine.close = close
        return engine

    m.engine_factory = delayed_close
    try:
        job = m.create("d" * 16, url(origin, "main.mkv"), {}, 0, 0, "tiles")
        src = src_of(m, job)
        assert closing.wait(timeout=5)
        m.cache.drop_tile(job.fp, 0)
        wait_for(lambda: 0 not in src.scheduler.done)
    finally:
        release.set()
    wait_for(lambda: len(src.events) >= 2)
    assert 0 in m.cache.entry(job.fp).tiles
