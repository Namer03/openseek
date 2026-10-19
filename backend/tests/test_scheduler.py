"""SCRUBBING.md §2 ordering, latest-wins and P2 pause, with a fake clock."""
from __future__ import annotations

from openseek.scheduler import Scheduler, Task, nearest_tile, tile_count


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def drain(s: Scheduler, n: int) -> list[Task]:
    out = []
    for _ in range(n):
        t = s.next()
        if t is None:
            break
        s.complete(t, ok=True)
        out.append(t)
    return out


def test_grid_helpers():
    assert tile_count(7_234_000) == 724 and tile_count(10_000) == 1 and tile_count(10_001) == 2
    assert nearest_tile(4_794_000, 724) == 479 and nearest_tile(4_796_000, 724) == 480
    assert nearest_tile(99_999_999, 724) == 723   # clamped to the last tile


def test_p0_target_then_neighbours_scrub_direction_first():
    c = Clock()
    s = Scheduler(724, "full", clock=c)
    s.set_priority(at_ms=None, playhead_ms=0)
    s.set_priority(at_ms=4_000_000, playhead_ms=0)     # first scrub: direction forward
    s.set_priority(at_ms=4_800_000, playhead_ms=0)     # forward again
    got = drain(s, 13)
    assert [t.k for t in got] == [480, 481, 479, 482, 478, 483, 477, 484, 476, 485, 475, 486, 474]
    assert all(t.cls == "P0" for t in got)
    s.set_priority(at_ms=3_000_000, playhead_ms=0)     # backwards scrub: backward neighbour first
    assert [t.k for t in drain(s, 3)] == [300, 299, 301]


def test_new_target_replaces_p0_queue_latest_wins():
    c = Clock()
    s = Scheduler(724, "full", clock=c)
    s.set_priority(at_ms=1_000_000, playhead_ms=0)
    first = s.next()
    assert first.k == 100
    s.set_priority(at_ms=5_000_000, playhead_ms=0)     # arrives while tile 100 is in flight
    assert s.next() is None                              # never two frames in flight
    s.complete(first, ok=True)                            # the in-flight frame is allowed to finish
    nxt = drain(s, 13)
    assert [t.k for t in nxt][:3] == [500, 501, 499]
    assert not any(90 <= t.k <= 110 for t in nxt)        # obsolete targets were dropped, not queued


def test_p1_window_then_p2_progressive_after_pause():
    c = Clock()
    s = Scheduler(724, "full", clock=c)
    s.set_priority(at_ms=None, playhead_ms=1_200_000)    # playhead at tile 120
    p1 = drain(s, 37)
    assert all(t.cls == "P1" for t in p1)
    assert sorted(t.k for t in p1) == list(range(102, 139))      # ±3 min = ±18 tiles
    assert [t.k for t in p1[:3]] == [120, 121, 119]
    p2 = drain(s, 200)
    assert all(t.cls == "P2" for t in p2)
    first_pass = [t.k for t in p2 if t.k % 6 == 0]
    assert p2[0].k % 6 == 0 and p2[0].k in (96, 144)               # nearest 60 s point outside the window
    sixty = [t.k for t in p2[:len([k for k in range(0, 724, 6) if not 102 <= k <= 138])]]
    assert all(k % 6 == 0 for k in sixty)                          # the whole 60 s pass precedes 30 s
    assert first_pass == sorted(first_pass, key=lambda k: (abs(k - 120), k < 120))


def test_p2_pauses_5s_after_p0_activity():
    c = Clock()
    s = Scheduler(40, "full", clock=c)
    s.set_priority(at_ms=None, playhead_ms=0)
    drain(s, 19)                                         # P1 covers 0..18
    s.set_priority(at_ms=250_000, playhead_ms=0)
    p0 = drain(s, 50)                                    # P0: 25 and its +-6 neighbours
    assert p0[0].k == 25 and all(t.cls == "P0" for t in p0)
    c.t += 1.0
    assert s.next() is None and s.seconds_until_p2() > 3.9
    c.t += 4.1
    t = s.next()
    assert t is not None and t.cls == "P2"


def test_pregen_modes():
    c = Clock()
    off = Scheduler(100, "off", clock=c)
    off.set_priority(None, 0)
    assert len(drain(off, 200)) == 19 and not off.complete_all
    ov = Scheduler(100, "overview", clock=c)
    ov.set_priority(None, 0)
    ks = [t.k for t in drain(ov, 200)]
    assert set(ks) == set(range(19)) | {k for k in range(0, 100, 6)}
    full = Scheduler(100, "full", clock=c)
    full.set_priority(None, 0)
    assert len(drain(full, 200)) == 100 and full.complete_all


def test_failures_and_lost_tiles():
    c = Clock()
    s = Scheduler(5, "full", clock=c)
    t = s.next()
    s.complete(t, ok=False)                     # transient: retried
    assert s.next().k == t.k
    s.complete(Task(t.k, t.cls), ok=False, permanent_failure=True)
    rest = drain(s, 10)
    assert t.k not in [x.k for x in rest] and s.complete_all
    s.mark_missing(3)                           # corrupt tile dropped from the cache
    assert not s.complete_all and s.next().k == 3


def test_done_tiles_from_cache_are_skipped():
    c = Clock()
    s = Scheduler(10, "full", done={0, 1, 2}, clock=c)
    assert 0 not in [t.k for t in drain(s, 20)]


def test_zero_pause_resumes_background_after_scrub_queue_drains():
    s = Scheduler(100, p2_pause_s=0, clock=Clock())
    drain(s, 19)
    s.set_priority(500_000, 0)
    assert all(t.cls == "P0" for t in drain(s, 13))
    assert s.next().cls == "P2"


def test_interleave_preserves_immediate_p0_and_bounds_p2_starvation():
    s = Scheduler(100, p2_pause_s=0, p1_interleave=3, clock=Clock())
    first = drain(s, 4)
    assert [t.cls for t in first] == ["P1", "P1", "P1", "P2"]
    assert first[-1].k == 24       # outside P1; do not spend its turn on nearby work
    s.set_priority(800_000, 0)
    assert s.next() == Task(80, "P0")
    s.complete(Task(80, "P0"), ok=True)
    assert all(t.cls == "P0" for t in drain(s, 12))
    assert [t.cls for t in drain(s, 4)] == ["P1", "P1", "P1", "P2"]


def test_interleave_still_obeys_pause_and_pregen_off():
    c = Clock()
    s = Scheduler(100, p1_interleave=1, clock=c)
    s.set_priority(500_000, 0)
    drain(s, 13)
    assert all(t.cls == "P1" for t in drain(s, 3))
    c.t += 5.1
    assert s.next().cls == "P2"
    off = Scheduler(100, pregen="off", p1_interleave=1, clock=c)
    assert all(t.cls == "P1" for t in drain(off, 100))


def test_p0_two_worker_continuous_scrub_lookahead():
    c = Clock()
    s = Scheduler(724, "full", max_inflight=2, clock=c)
    s.set_priority(at_ms=200_000, playhead_ms=0)        # tile 20: initial step -> exact k0 first
    assert [t.k for t in drain(s, 2)] == [20, 21]
    c.t += 0.15
    s.set_priority(at_ms=220_000, playhead_ms=0)        # tile 22: continuous forward step -> lookahead +2 then k0
    w0 = s.next()
    w1 = s.next()
    assert w0 == Task(24, "P0") and w1 == Task(22, "P0")
    s.complete(w0, ok=True)
    s.complete(w1, ok=True)
    c.t += 0.15
    s.set_priority(at_ms=500_000, playhead_ms=0)        # tile 50: large jump resets streak -> exact k0 first
    assert s.next() == Task(50, "P0")


def test_hierarchical_coarse_to_fine_full_timeline_first():
    c = Clock()
    s = Scheduler(143, "full", max_inflight=2, hierarchical=True, clock=c)
    s.set_priority(at_ms=None, playhead_ms=0)
    # Stage 0: immediate playhead seed (0, 1, 2)
    seed = drain(s, 3)
    assert [t.k for t in seed] == [0, 1, 2]
    # Stage 1: progressive 60s pass (k % 6 == 0, plus end tile 142) finishes completely before 30s or 10s
    g60 = {k for k in range(0, 143, 6)} | {142}
    rem_60 = len(g60 - {0, 1, 2})
    pass_60 = drain(s, rem_60)
    assert {0, 1, 2} | {t.k for t in pass_60} == g60 | {1, 2}
    # Verify progressive bisection: first 6 tiles of pass_60 cover the whole 24-min episode at 240s spacing + end tile
    assert set(t.k for t in pass_60[:6]) == {24, 48, 72, 96, 120, 142}
    # Stage 2: 30s pass (k % 3 == 0) finishes completely before remaining 10s tiles
    g30 = {k for k in range(0, 143, 3)}
    rem_30 = len(g30 - s.done)
    pass_30 = drain(s, rem_30)
    assert g30.issubset(s.done) and len(pass_30) == rem_30
    # Stage 3 & 4: remaining 10s tiles complete the full 143-tile grid
    rest = drain(s, 200)
    assert s.complete_all and len(s.done) == 143 and len(rest) == 143 - len(g30 | {1, 2, 142})


def test_hierarchical_coarse_passes_are_byte_paced_but_seed_and_p0_are_not():
    s = Scheduler(143, "full", max_inflight=2, hierarchical=True)
    seed = drain(s, 3)
    assert all(t.cls == "P1" and not t.bg for t in seed)          # playhead seed stays foreground
    coarse = drain(s, 20)
    assert all(t.cls == "P1" and t.bg for t in coarse)            # 60s/30s passes are byte-paced
    s.set_priority(1_000_000)
    p0 = s.next()
    assert p0.cls == "P0" and not p0.bg
