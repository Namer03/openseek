"""SwitchEngine routing/safety rules from the independent review, with fake workers (no processes, no network)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from openseek import timeline as tl
from openseek.altpipeline import AltState, SwitchEngine
from openseek.fetch import UpstreamError
from openseek.governor import Governor, Limits
from openseek.worker import GrabResult, WorkerError


class FakeEngine:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    pid = 1

    def is_open(self):
        return True

    def open(self):
        return None

    def kill(self):
        pass

    def close(self):
        pass

    def grab(self, t_ms, accurate, tolerance_ms=None):
        self.calls.append((t_ms, accurate))
        r = self.script.pop(0) if self.script else GrabResult(t_ms, b"jpeg-" + str(t_ms).encode(), 1, 100)
        if isinstance(r, Exception):
            raise r
        return r


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def rig(mapping=None, **limits):
    clock = Clock()
    gov = Governor(Limits(**limits), clock=clock)
    src = SimpleNamespace(stop_flag=False, urgent=False, governor=gov, alt=None, alt_slots=set(), fp="f" * 64)
    deact = []
    mgr = SimpleNamespace(clock=clock, settings=SimpleNamespace(), alt_deactivate=lambda s, why, final=True: (
        deact.append(why), setattr(s, "alt", None)))
    eng = SwitchEngine.__new__(SwitchEngine)
    eng.mgr, eng.src = mgr, src
    eng.a, eng.b, eng._b_for = FakeEngine(), FakeEngine(), "c1"
    eng._b_engine = lambda alt: eng.b
    mp = mapping or tl.Mapping(1.0, 7000.0, 30.0, 8, "offset", 500.0)
    alt = AltState("c1", mp, "b" * 64, SimpleNamespace(), SimpleNamespace(last_error=None), "cap", "cdn", "matroska",
                   1, 3500, since=clock())
    src.alt = alt
    return eng, src, alt, gov, clock, deact


def test_maps_time_both_ways_and_marks_alt_slot():
    eng, src, alt, gov, clock, _ = rig()
    gov.playback(20_000, False, "cdn")
    r = eng.grab(100_000, False)
    assert eng.b.calls == [(107_000, False)] and r.via == "b" and r.pts_ms == 100_000   # B time = A time + 7 s
    assert src.alt_slots == {10}


def test_empty_frame_from_b_is_retried_accurately_not_lost():
    eng, src, alt, gov, clock, _ = rig()
    gov.playback(20_000, False, "cdn")
    eng.b.script = [GrabResult(106_000, b"", 1, 10), GrabResult(107_000, b"frame", 2, 900)]
    r = eng.grab(100_000, False)
    assert eng.b.calls == [(107_000, False), (107_000, True)] and r.jpeg == b"frame" and r.via == "b"


def test_retry_after_sends_tiles_to_playback_source_instead_of_stalling():
    eng, src, alt, gov, clock, _ = rig()
    gov.playback(20_000, False, "cdn")
    gov.retry_after("cdn", 3600)
    r = eng.grab(50_000, False)
    assert r.via == "a" and eng.b.calls == [] and eng.a.calls == [(50_000, False)]


def test_urgent_bypasses_only_the_concurrency_cap_never_a_pause_or_rebuffer():
    eng, src, alt, gov, clock, _ = rig(max_conc=1, host_budget=2)
    src.urgent = True
    gov.playback(20_000, False, "cdn", playback_conns=2)           # host budget used up by playback: status "full"
    assert gov.status("cdn") == "full"
    assert eng.grab(10_000, False).via == "b"                       # urgent may pass the cap
    gov.retry_after("cdn", 30)
    assert eng.grab(20_000, False).via == "a"                       # but not a Retry-After pause
    clock.t += 31
    gov.playback(100, True, "cdn")                                  # rebuffering: wait, do not push
    done = []
    th = threading.Thread(target=lambda: done.append(eng.grab(30_000, False)), daemon=True)
    th.start()
    th.join(0.5)
    assert th.is_alive()                                            # still waiting for playback to recover
    gov.playback(20_000, False, "cdn")
    th.join(3)
    assert done and done[0].via == "b"


def test_telemetry_lost_ends_the_negotiation_and_returns_to_playback_source():
    eng, src, alt, gov, clock, deact = rig()
    gov.playback(20_000, False, "cdn")
    clock.t += gov.l.telemetry_lost_s + 1
    r = eng.grab(10_000, False)
    assert r.via == "a" and deact == ["telemetry_lost"] and src.alt is None


def test_no_telemetry_ever_gives_up_after_the_grace_period():
    eng, src, alt, gov, clock, deact = rig()
    clock.t += SwitchEngine.TELEMETRY_GRACE_S + 1                    # client never reported
    assert eng.grab(10_000, False).via == "a" and deact == ["telemetry_lost"]


def test_three_consecutive_b_failures_trip_the_circuit_breaker_and_each_tile_still_arrives():
    eng, src, alt, gov, clock, deact = rig()
    gov.playback(20_000, False, "cdn")
    eng.b.script = [WorkerError("internal", "x")] * 3
    out = [eng.grab(k * 10_000, False) for k in (1, 2, 3)]
    assert all(r.via == "a" for r in out) and deact and deact[0].startswith("failed:")
    assert eng.b.calls and len(eng.a.calls) == 3                     # no tile was lost, B stopped being tried


def test_fatal_expired_link_deactivates_immediately():
    eng, src, alt, gov, clock, deact = rig()
    gov.playback(20_000, False, "cdn")
    alt.proxy_src.last_error = UpstreamError("source_expired", "http_403")
    eng.b.script = [WorkerError("upstream", "")]
    # the fake engine does not clear last_error; the real SwitchEngine resets it before each B grab, so set it in-flight
    orig = eng.b.grab

    def grab(t, a, tol=None):
        alt.proxy_src.last_error = UpstreamError("source_expired", "http_403")
        return orig(t, a, tol)
    eng.b.grab = grab
    assert eng.grab(10_000, False).via == "a" and deact == ["failed:http_403"]


def test_stale_error_state_is_cleared_before_each_b_grab():
    eng, src, alt, gov, clock, deact = rig()
    gov.playback(20_000, False, "cdn")
    alt.proxy_src.last_error = UpstreamError("source_expired", "http_403")   # left over from an earlier tile
    assert eng.grab(10_000, False).via == "b" and alt.proxy_src.last_error is None and not deact


@pytest.mark.parametrize("retry_after", [0.0, 12.0])
def test_a_429_always_pauses_b_even_with_retry_after_zero(retry_after):
    eng, src, alt, gov, clock, deact = rig()
    gov.playback(20_000, False, "cdn")

    def grab(t, a, tol=None):
        e = UpstreamError("source_unreachable", "http_429")
        e.retry_after = retry_after
        alt.proxy_src.last_error = e
        raise WorkerError("upstream", "")
    eng.b.grab = grab
    assert eng.grab(10_000, False).via == "a"                       # this tile still arrives, from the playback source
    assert gov.status("cdn") == "pause" and not deact               # B paused (>= 1 s even for 0), not deactivated
    assert gov.stats()["retry_after_events"] == 1
    assert eng.grab(20_000, False).via == "a"                       # and B is not touched while paused
