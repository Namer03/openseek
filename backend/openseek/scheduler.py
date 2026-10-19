"""Per-source generation order, as described in docs/ARCHITECTURE.md (pure logic, no I/O).

Grid: tile k is the frame for k * 10 s. Priority classes, highest first:
  P0  the tile for the latest scrub target `at_ms`, then neighbours ±1 … ±6, alternating,
      scrub direction first. A new `at_ms` REPLACES the P0 queue (latest wins).
  P1  tiles within ±3 min of the playhead, nearest first.
  P2  overview: 60 s grid, then 30 s, then 10 s, each pass by distance from the playhead.
      P2 pauses for 5 s after any P0 activity and obeys OPENSEEK_PREGEN (off|overview|full).
One frame in flight per source: next() hands out a single task until complete() is called.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

GRID_MS = 10_000
P0_RADIUS = 6
P1_WINDOW_MS = 180_000
P2_PAUSE_S = 5.0


@dataclass(frozen=True)
class Task:
    k: int
    cls: str  # "P0" | "P1" | "P2"
    bg: bool = False  # byte-paced like P2 although cls is "P1" (hierarchical coarse passes)

    @property
    def t_ms(self) -> int:
        return self.k * GRID_MS


def tile_count(duration_ms: int) -> int:
    """Tiles k with k*10 s < duration."""
    if duration_ms <= 0:
        return 0
    return (duration_ms - 1) // GRID_MS + 1


def nearest_tile(at_ms: int, n_tiles: int) -> int:
    k = (max(0, at_ms) + GRID_MS // 2) // GRID_MS
    return max(0, min(n_tiles - 1, k))


class Scheduler:
    def __init__(self, n_tiles: int, pregen: str = "full", done: set[int] | None = None,
                 clock: Callable[[], float] = time.monotonic, p0_radius: int = P0_RADIUS,
                 p2_pause_s: float = P2_PAUSE_S, p1_interleave: int = 0, max_inflight: int = 1,
                 hierarchical: bool = False):
        if pregen not in ("off", "overview", "full"):
            raise ValueError("pregen")
        self.n = n_tiles
        self.pregen = pregen
        self.clock = clock
        self.p0_radius = p0_radius
        self.p2_pause_s = p2_pause_s
        self.p1_interleave = p1_interleave
        self.max_inflight = max(1, int(max_inflight))
        self.hierarchical = bool(hierarchical)
        self.p1_streak = 0
        self.scrub_streak = 0
        self.last_direction = 1
        self.done: set[int] = set(k for k in (done or ()) if 0 <= k < n_tiles)
        self.failed: set[int] = set()
        self.inflight_tasks: dict[int, Task] = {}
        self.p0: list[int] = []
        self.at_ms: int | None = None
        self.playhead_ms = 0
        self.last_p0_activity = -1e18

    @property
    def inflight(self) -> Task | None:
        return next(iter(self.inflight_tasks.values()), None)

    @inflight.setter
    def inflight(self, value: Task | None) -> None:
        self.inflight_tasks.clear()
        if value is not None:
            self.inflight_tasks[value.k] = value

    # ---------------------------------------------------------------- inputs
    def set_priority(self, at_ms: int | None, playhead_ms: int | None = None) -> None:
        now = self.clock()
        if playhead_ms is not None:
            self.playhead_ms = max(0, int(playhead_ms))
        if at_ms is None:
            if self.at_ms is not None or self.p0:
                self.last_p0_activity = now     # scrubbing ended: P2 waits 5 s from here
            self.p0 = []
            self.at_ms = None
            self.scrub_streak = 0
            return
        at_ms = max(0, int(at_ms))
        prev_at = self.at_ms
        prev_time = self.last_p0_activity
        direction = -1 if (prev_at is not None and at_ms < prev_at) else (
            1 if (prev_at is not None and at_ms > prev_at) else self.last_direction
        )
        self.at_ms = at_ms
        self.last_p0_activity = now
        if self.n == 0:
            self.p0 = []
            return
        k0 = nearest_tile(at_ms, self.n)
        if prev_at is not None:
            prev_k0 = nearest_tile(prev_at, self.n)
            dk = abs(k0 - prev_k0)
            dt = now - prev_time
            if 1 <= dk <= 4 and direction == self.last_direction and 0.0 <= dt <= 0.6:
                self.scrub_streak += 1
            elif dk > 4 or direction != self.last_direction or dt > 0.6:
                self.scrub_streak = 0
        else:
            self.scrub_streak = 0
        self.last_direction = direction
        if self.scrub_streak >= 1 and self.max_inflight > 1:
            candidates = [k0 + 2 * direction, k0, k0 + 3 * direction, k0 + 1 * direction]
            for d in range(1, self.p0_radius + 1):
                candidates.append(k0 + d * direction)
                candidates.append(k0 - d * direction)
            order: list[int] = []
            seen_p0: set[int] = set()
            for k in candidates:
                if 0 <= k < self.n and k not in seen_p0:
                    order.append(k)
                    seen_p0.add(k)
        else:
            order = [k0]
            for d in range(1, self.p0_radius + 1):
                for k in (k0 + d * direction, k0 - d * direction):
                    if 0 <= k < self.n:
                        order.append(k)
        self.p0 = order   # replaces (never appends to) the previous P0 queue

    def mark_done(self, k: int) -> None:
        if 0 <= k < self.n:
            self.done.add(k)
            self.failed.discard(k)

    def mark_missing(self, k: int) -> None:
        """A published tile was lost (corrupt file): make it eligible again."""
        self.done.discard(k)

    # ---------------------------------------------------------------- queue
    def _pending(self, k: int) -> bool:
        return k not in self.done and k not in self.failed and k not in self.inflight_tasks

    def _pick_pending(self, ks: list[int], cls: str, bg: bool = False) -> Task | None:
        first_pending: int | None = None
        for k in ks:
            if not self._pending(k):
                continue
            if first_pending is None:
                first_pending = k
            if (k - 1) not in self.inflight_tasks:
                return Task(k, cls, bg)
        return Task(first_pending, cls, bg) if first_pending is not None else None

    def _p1_seed(self) -> list[int]:
        if self.n == 0:
            return []
        c = nearest_tile(self.playhead_ms, self.n)
        out = [c]
        for d in (1, 2):
            for k in (c + d, c - d):
                if 0 <= k < self.n:
                    out.append(k)
        return out

    def _p1(self) -> list[int]:
        if self.n == 0:
            return []
        c = nearest_tile(self.playhead_ms, self.n)
        w = P1_WINDOW_MS // GRID_MS
        out = [c]
        for d in range(1, w + 1):
            for k in (c + d, c - d):
                if 0 <= k < self.n:
                    out.append(k)
        return out

    def _hierarchical_coarse_60s(self) -> list[int]:
        if self.n == 0:
            return []
        c = nearest_tile(self.playhead_ms, self.n)
        strides = (48, 24, 12, 6) if self.n >= 240 else (24, 12, 6)
        out: list[int] = []
        seen: set[int] = set()
        for idx, step in enumerate(strides):
            ks = [k for k in range(0, self.n, step) if k not in seen]
            ks.sort(key=lambda k: (abs(k - c), k < c))
            out += ks
            seen.update(ks)
            if idx == 0 and (self.n - 1) not in seen:
                out.append(self.n - 1)
                seen.add(self.n - 1)
        return out

    def _hierarchical_coarse_30s(self) -> list[int]:
        if self.n == 0:
            return []
        c = nearest_tile(self.playhead_ms, self.n)
        ks_even: list[int] = []
        ks_odd: list[int] = []
        for idx, k in enumerate(range(3, self.n, 6)):
            if idx % 2 == 0:
                ks_even.append(k)
            else:
                ks_odd.append(k)
        ks_even.sort(key=lambda k: (abs(k - c), k < c))
        ks_odd.sort(key=lambda k: (abs(k - c), k < c))
        return ks_even + ks_odd

    def _hierarchical_fine_10s(self) -> list[int]:
        if self.n == 0:
            return []
        c = nearest_tile(self.playhead_ms, self.n)
        n_blocks = (self.n - 1) // 3 + 1
        blocks = list(range(n_blocks))
        blocks.sort(key=lambda m: (abs(3 * m + 1 - c), 3 * m + 1 < c))
        out: list[int] = []
        for m in blocks:
            for r in (1, 2):
                k = 3 * m + r
                if k < self.n:
                    out.append(k)
        return out

    def _p2(self) -> list[int]:
        if self.pregen == "off" or self.n == 0:
            return []
        c = nearest_tile(self.playhead_ms, self.n)
        passes = [6] if self.pregen == "overview" else [6, 3, 1]
        out: list[int] = []
        seen: set[int] = set()
        for step in passes:
            ks = [k for k in range(0, self.n, step) if k not in seen]
            ks.sort(key=lambda k: (abs(k - c), k < c))
            out += ks
            seen.update(ks)
        return out

    def p2_paused(self) -> bool:
        return bool(self.p0) or (self.clock() - self.last_p0_activity) < self.p2_pause_s

    def peek(self) -> Task | None:
        for k in self.p0:
            if self._pending(k):
                return Task(k, "P0")
        if self.hierarchical and self.pregen in ("overview", "full"):
            # Stage 0: immediate playhead seed (±20s)
            t = self._pick_pending(self._p1_seed(), "P1")
            if t is not None:
                return t
            # Stage 1: progressive full-timeline 60-second grid (never paused by p2_pause_s)
            t = self._pick_pending(self._hierarchical_coarse_60s(), "P1", bg=True)
            if t is not None:
                return t
            # Stage 2: progressive full-timeline 30-second grid (when pregen="full")
            if self.pregen == "full":
                t = self._pick_pending(self._hierarchical_coarse_30s(), "P1", bg=True)
                if t is not None:
                    return t
            # Stage 3: near-playhead 10-second window (±3 min)
            t = self._pick_pending(self._p1(), "P1")
            if t is not None:
                return t
            # Stage 4: full-timeline 10-second refinement (pauses during/after P0 scrub)
            if self.pregen == "full" and not self.p2_paused():
                t = self._pick_pending(self._hierarchical_fine_10s(), "P2")
                if t is not None:
                    return t
            return None
        if (self.p1_interleave and self.p1_streak >= self.p1_interleave
                and not self.p2_paused()):
            nearby = set(self._p1())
            for k in self._p2():
                if k not in nearby and self._pending(k):
                    return Task(k, "P2")
        for k in self._p1():
            if self._pending(k):
                return Task(k, "P1")
        if not self.p2_paused():
            for k in self._p2():
                if self._pending(k):
                    return Task(k, "P2")
        return None

    def lookahead(self, n: int) -> list[Task]:
        """The next up to n tasks next() would hand out, WITHOUT claiming them (used to prefetch their bytes)."""
        picked: list[Task] = []
        try:
            for _ in range(max(0, n)):
                t = self.peek()
                if t is None:
                    break
                picked.append(t)
                self.inflight_tasks[t.k] = t            # temporary: makes peek() move on to the following task
        finally:
            for t in picked:
                self.inflight_tasks.pop(t.k, None)
        return picked

    def next(self) -> Task | None:
        if len(self.inflight_tasks) >= self.max_inflight:
            return None                      # bounded frames in flight per source
        # drop P0 entries that are finished so the queue drains
        self.p0 = [k for k in self.p0 if k not in self.done and k not in self.failed]
        t = self.peek()
        if t is not None:
            self.inflight_tasks[t.k] = t
        return t

    def complete(self, task: Task, ok: bool, permanent_failure: bool = False) -> None:
        self.inflight_tasks.pop(task.k, None)
        if ok:
            self.mark_done(task.k)
            if task.cls == "P1":
                self.p1_streak += 1
            elif task.cls == "P2":
                self.p1_streak = 0
        elif permanent_failure:
            self.failed.add(task.k)
        if task.cls == "P0":
            self.last_p0_activity = self.clock()
            if task.k in self.p0 and (ok or permanent_failure):
                self.p0.remove(task.k)

    def has_p0(self) -> bool:
        return any(self._pending(k) for k in self.p0)

    def seconds_until_p2(self) -> float:
        if self.p0:
            return self.p2_pause_s
        return max(0.0, self.p2_pause_s - (self.clock() - self.last_p0_activity))

    @property
    def complete_all(self) -> bool:
        return self.n > 0 and len(self.done) + len(self.failed) >= self.n
