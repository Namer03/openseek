"""Playback-aware pausing of background extraction (default OFF): unit logic with a fake clock, then the real server."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from helpers import wait_for
from origin import Origin
from openseek.config import Settings
from openseek.fetch import UpstreamStats
from openseek.governor import Governor, Limits
from openseek.guard import GuardLimits, PlaybackGuard
from test_integration import _create, _policy, _status, _url
from test_scenarios import _register


class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(stall=None, limits: GuardLimits | None = None):
    clk = Clock()
    g = Governor(Limits(), clock=clk)
    sv = {"s": 0.0} if stall is None else stall
    return clk, g, sv, PlaybackGuard(g, lambda: sv["s"], clk, limits or GuardLimits())


def report(clk, g, n=1, rebuffering=False, seeking=False, buffer_ms=20_000):
    for _ in range(n):
        clk.t += 1.0
        g.playback(buffer_ms, rebuffering, "h", 1, seeking)


def test_default_is_off():
    assert Settings().playback_guard is False
    assert Settings.from_env({"OPENSEEK_PLAYBACK_GUARD": "1"}).playback_guard is True


def test_no_telemetry_stream_means_nothing_to_protect_against_rebuffer_or_stale():
    clk, g, sv, guard = make()
    clk.t += 500
    assert guard.check() is None and guard.worker_slot_free(5)


def test_startup_and_seek_buffering_never_pause():
    clk, g, sv, guard = make()
    report(clk, g, 2)
    report(clk, g, 3, rebuffering=True)                      # start-up: playback was not yet stable
    assert guard.check() is None
    report(clk, g, 12)                                       # steady playback
    report(clk, g, 5, rebuffering=True, seeking=True)        # the person scrubbed
    assert guard.check() is None
    assert guard.stats()["pauses"] == {"rebuffer": 0, "stale": 0, "stall": 0}


def test_buffering_after_a_seek_that_never_ends_is_a_stall_after_all():
    clk, g, sv, guard = make()
    report(clk, g, 12)
    report(clk, g, 15, rebuffering=True, seeking=True)
    assert guard.check() is None
    report(clk, g, 8, rebuffering=True, seeking=True)        # > 20 s of continuous buffering
    assert guard.check() == "rebuffer"


def test_genuine_rebuffer_pauses_holds_after_recovery_then_ramps_up_with_one_worker():
    clk, g, sv, guard = make()
    report(clk, g, 12)
    assert guard.check() is None
    report(clk, g, 1, rebuffering=True)                      # a real stall in the middle of steady playback
    assert guard.check() == "rebuffer"
    for _ in range(4):                                       # workers poll continuously: every poll renews the hold
        report(clk, g, 1, rebuffering=True)
        assert guard.check() == "rebuffer"
    report(clk, g, 1)                                        # the player is back
    for _ in range(15):                                      # ... but background work stays held for the hold time
        report(clk, g, 1)
        assert guard.check() == "rebuffer"
    for _ in range(6):
        report(clk, g, 1)
        guard.check()
    assert guard.check() is None                             # 20 s after the last bad report
    assert guard.stats()["ramp_left"] == 10
    assert guard.worker_slot_free(0) and not guard.worker_slot_free(1)         # one worker only
    for _ in range(10):
        guard.note_tile_done()
    assert guard.worker_slot_free(2) and guard.stats()["paused_total_s"] > 20


def test_lost_telemetry_keeps_background_work_paused_until_healthy_telemetry_returns():
    clk, g, sv, guard = make()
    report(clk, g, 5)
    clk.t += 5
    assert guard.check() is None                             # 5 s of silence is not stale yet
    clk.t += 6
    assert guard.check() == "stale"                          # > 10 s
    for minutes in (1, 5, 30, 120):                          # silence is not "playback ended": still paused, however long
        clk.t += minutes * 60
        assert guard.check() == "stale"
    report(clk, g, 3)                                        # healthy telemetry is back ...
    assert guard.check() == "stale"                          # ... the hold still applies
    report(clk, g, 12)
    assert guard.check() is None                             # then work resumes (with the one-worker ramp)
    assert guard.stats()["ramp_left"] == 10


def test_cdn_stall_pauses_with_doubling_cooldown_and_forgets_after_a_calm_period():
    clk, g, sv, guard = make()
    holds = []
    for _ in range(4):
        sv["s"] = 7.0
        assert guard.check() == "stall"
        sv["s"] = 0.0
        t0 = clk.t
        while guard.check() is not None:
            clk.t += 1
        holds.append(round(clk.t - t0))
        clk.t += 5
    assert holds[0] in (30, 31) and holds[1] in (60, 61) and holds[2] in (120, 121) and holds[3] in (120, 121)
    clk.t += 700
    sv["s"] = 7.0
    assert guard.check() == "stall"
    sv["s"] = 0.0
    t0 = clk.t
    while guard.check() is not None:
        clk.t += 1
    assert round(clk.t - t0) in (30, 31)


def test_upstream_stall_seconds_track_waiting_requests_and_stuck_bodies():
    st = UpstreamStats()
    assert st.stall_seconds() == 0.0
    tok = st.begin()
    t0 = time.monotonic()
    assert st.stall_seconds(now=t0 + 8) >= 7.5
    st.end(206, 0.1, tok)
    assert st.stall_seconds(now=t0 + 60) == 0.0
    st.body_begin()
    assert st.stall_seconds(now=time.monotonic() + 9) >= 8.5
    st.body_progress()
    assert st.stall_seconds() < 1.0
    st.body_end()
    assert st.stall_seconds(now=time.monotonic() + 60) == 0.0


# ------------------------------------------------------------------------------------------- the real server
from helpers import FIXTURES as ALT, FIXTURES_HINT
FILE = "ok_10min.mkv"
needs_fixture = pytest.mark.skipif(not (ALT / FILE).exists(), reason=FIXTURES_HINT)


class Rig:
    """A live server with the guard on (every guard timer scaled 10x faster), a fixture origin and a fake player."""

    def __init__(self, live_factory, guard: bool = True, rps: float = 3.0, latency: float = 0.1, stall_s: float = 10.0):
        self.o = Origin(ALT).start()
        self.o.reset(latency_s=latency)
        self.srv = live_factory(_policy(self.o), pregen="full", workers_per_source=2, upstream_rps=rps, bg_mbps=64.0,
                                playback_guard=guard, guard_stall_s=stall_s, guard_time_scale=0.1, create_wait_s=10.0)
        tok, _ = _register(self.srv)
        self.c = self.srv.client(tok)
        self.state = {"rebuffering": False, "seeking": False, "post": True, "buffer_ms": 20_000}
        self.stop = threading.Event()
        self.pid = None

    def start(self) -> str:
        self.pid = _create(self.c, _url(self.o, FILE), at_ms=0, layout="tiles")["preview_id"]
        threading.Thread(target=self._player, daemon=True).start()
        return self.pid

    def _player(self) -> None:
        while not self.stop.is_set():
            if self.state["post"]:
                try:
                    self.c.post(f"/v1/previews/{self.pid}/telemetry", json={
                        "buffer_ms": self.state["buffer_ms"], "rebuffering": self.state["rebuffering"], "host": "h", "conns": 1})
                except Exception:  # noqa: BLE001
                    pass
            self.stop.wait(0.1)

    @property
    def src(self):
        return next(iter(self.srv.core.jobs.sources.values()))

    def done(self) -> int:
        return _status(self.c, self.pid)["progress"]["tiles_done"]

    def complete(self, timeout: float = 120.0):
        return wait_for(lambda: _status(self.c, self.pid)["state"] == "complete" and True, timeout, 0.1)

    def close(self) -> None:
        self.stop.set()
        self.c.close()
        self.o.stop()


@needs_fixture
def test_healthy_playback_is_never_paused_and_the_grid_completes(live_factory):
    r = Rig(live_factory)
    try:
        r.start()
        wait_for(lambda: r.done() >= 5, 30, 0.1)
        r.complete()
        s = r.src.guard.stats()
        assert s["pauses"] == {"rebuffer": 0, "stall": 0, "stale": 0} and r.done() == r.src.scheduler.n
        with r.srv.admin_client() as a:                                  # the guard's telemetry is visible to the operator
            tel = a.get("/admin/metrics").json()["sources"][0]["telemetry"]
        assert tel["reports"] >= 10 and tel["telemetry_age_s"] is not None and tel["telemetry_age_s"] < 5
    finally:
        r.close()


@needs_fixture
def test_genuine_rebuffer_stops_background_work_keeps_urgent_scrubs_and_resumes(live_factory):
    r = Rig(live_factory)
    try:
        r.start()
        wait_for(lambda: r.done() >= 8 and r.src.governor.reports >= 12, 30, 0.1)     # steady playback is established
        r.state["rebuffering"] = True
        wait_for(lambda: r.src.guard.stats()["paused"] == "rebuffer", 5, 0.05)
        time.sleep(0.8)                                                                 # in-flight work drains
        before = r.done()
        time.sleep(1.5)
        assert r.done() - before <= 1, "background extraction kept running during a genuine rebuffer"
        urgent_k = r.src.scheduler.n - 3                                                # far away: a scrub target
        r.c.post(f"/v1/previews/{r.pid}/priority", json={"at_ms": urgent_k * 10_000, "playhead_ms": 0})
        assert wait_for(lambda: r.srv.core.jobs.cache.tile_bytes(r.src.fp, urgent_k) is not None, 10, 0.1), \
            "an urgent scrub tile must be produced even while background work is held"
        r.state["rebuffering"] = False
        assert wait_for(lambda: r.src.guard.stats()["paused"] is None, 10, 0.1)
        assert r.src.guard.stats()["ramp_left"] > 0 or r.done() > before
        r.complete()
        assert r.src.guard.stats()["pauses"]["rebuffer"] == 1 and r.done() == r.src.scheduler.n
    finally:
        r.close()


@needs_fixture
def test_buffering_right_after_a_seek_does_not_pause(live_factory):
    r = Rig(live_factory)
    try:
        r.start()
        wait_for(lambda: r.done() >= 8 and r.src.governor.reports >= 12, 30, 0.1)
        r.c.post(f"/v1/previews/{r.pid}/priority", json={"at_ms": 300_000, "playhead_ms": 300_000})     # a scrub / jump
        r.state["rebuffering"] = True
        time.sleep(1.2)                                                                  # < the (scaled) seek allowance
        r.state["rebuffering"] = False
        r.complete()
        assert r.src.guard.stats()["pauses"]["rebuffer"] == 0
    finally:
        r.close()


@needs_fixture
def test_silent_telemetry_keeps_extraction_paused_past_the_lost_window_and_resumes_when_it_returns(live_factory):
    r = Rig(live_factory)
    try:
        r.start()
        wait_for(lambda: r.done() >= 8 and r.src.governor.reports >= 5, 30, 0.1)
        r.state["post"] = False                                                          # the player went silent
        assert wait_for(lambda: r.src.guard.stats()["paused"] == "stale", 6, 0.05)
        time.sleep(5.0)                                                                  # > the (scaled) 3 s lost window
        before = r.done()
        time.sleep(2.0)
        assert r.src.guard.stats()["paused"] == "stale", "must not resume merely because time passed"
        assert r.done() - before <= 1
        r.state["post"] = True                                                           # healthy telemetry returns
        assert wait_for(lambda: r.src.guard.stats()["paused"] is None, 15, 0.1)
        r.complete(90)
        assert r.src.guard.stats()["pauses"]["stale"] >= 1 and r.done() == r.src.scheduler.n
    finally:
        r.close()


@needs_fixture
def test_a_confirmed_end_of_playback_stops_the_source_even_while_paused(live_factory):
    r = Rig(live_factory)
    try:
        r.start()
        wait_for(lambda: r.done() >= 8 and r.src.governor.reports >= 5, 30, 0.1)
        r.state["post"] = False
        assert wait_for(lambda: r.src.guard.stats()["paused"] == "stale", 6, 0.05)
        src = r.src
        assert r.c.delete(f"/v1/previews/{r.pid}").status_code == 204                 # the client's courtesy DELETE on exit
        assert wait_for(lambda: src.stop_flag and not src.running, 10, 0.1), "the last job going away must stop the source"
    finally:
        r.close()


@needs_fixture
def test_enabling_the_client_telemetry_flag_does_not_activate_alternative_sources(live_factory):
    """The Shield's openseek-alt.txt makes the client send an alt block and telemetry. With alt_source OFF (production)
    only telemetry is used: no negotiation, no link is ever wanted or accepted, nothing but the playback file is fetched."""
    r = Rig(live_factory)
    try:
        assert r.srv.core.settings.alt_source is False
        alt_block = {"play": {"cid": "p", "title_key": "tt1", "height": 2160, "duration_ms": 600_000, "size": 9_000_000_000,
                              "codec": "hevc", "container": "mkv", "name": "x.2160p", "cached": True, "resolved": True},
                     "candidates": [{"cid": "c1", "title_key": "tt1", "height": 720, "duration_ms": 600_000, "size": 1_000_000_000,
                                     "codec": "h264", "container": "mkv", "name": "x.720p.BluRay", "cached": True, "resolved": True}],
                     "telemetry": True}
        resp = r.c.post("/v1/previews", json={"source": {"url": _url(r.o, FILE)}, "playhead_ms": 0, "layout": "tiles", "alt": alt_block})
        assert resp.status_code == 202
        r.pid = resp.json()["preview_id"]
        threading.Thread(target=r._player, daemon=True).start()
        wait_for(lambda: r.done() >= 6 and r.src.governor.reports >= 5, 30, 0.1)
        body = _status(r.c, r.pid)
        assert not body.get("alt"), "no alternative-source negotiation may appear in the status"
        assert r.c.post(f"/v1/previews/{r.pid}/alt-source", json={"cid": "c1", "source": {"url": _url(r.o, FILE)}}).status_code == 409
        assert r.srv.core.jobs.sources and r.src.alt is None and r.src.governor.reports >= 5       # telemetry IS used
        r.complete()
        paths = {x["path"].split("?")[0] for x in r.o.log}
        assert paths == {f"/f/{FILE}"}, f"unexpected CDN requests: {paths}"
        assert r.src.upstream.stats.snapshot()["resolutions"] == 1                                   # the playback file only
    finally:
        r.close()


@needs_fixture
def test_a_sustained_cdn_stall_pauses_and_work_resumes_after_the_cooldown(live_factory):
    r = Rig(live_factory, stall_s=10.0)
    try:
        r.start()
        wait_for(lambda: r.done() >= 6, 30, 0.1)
        r.o.reset(latency_s=2.5)                                                         # every request now hangs 2.5 s (> 1 s)
        assert wait_for(lambda: r.src.guard.stats()["paused"] == "stall", 10, 0.05)
        r.o.reset(latency_s=0.1)
        r.complete(120)
        s = r.src.guard.stats()
        assert s["pauses"]["stall"] >= 1 and r.done() == r.src.scheduler.n
    finally:
        r.close()


@needs_fixture
def test_with_the_guard_off_nothing_changes(live_factory):
    r = Rig(live_factory, guard=False)
    try:
        r.start()
        r.complete()
        assert r.src.guard is None and r.done() == r.src.scheduler.n
    finally:
        r.close()
