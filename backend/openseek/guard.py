"""Playback-aware pausing of BACKGROUND extraction (default off: Settings.playback_guard).

Pauses new background work (P1/P2) on:
  * a genuine rebuffer: buffering in the middle of steady playback (the Governor already tells seeks and start-up
    buffering apart; those are never a pause, unless they never end: > seek_buffer_max_s of continuous buffering),
  * stale telemetry, once a telemetry stream had been established and is then missing. Silence never means "playback
    ended": the hold lasts until healthy telemetry is back. The ways a session really ends stop the source itself and
    need no help from here: the client's DELETE on exit (the last job goes -> source.stop("cancel")) or no client
    activity for idle_timeout_s (-> source.stop("idle")). A stream that never existed does nothing (nothing to protect),
  * a sustained CDN stall: a request that has waited for its first byte, or a body that stopped moving, for stall_s.
Urgent work (P0: the person is scrubbing) is never held. In-flight requests finish; nothing is aborted.

Recovery is conservative: a pause lasts at least a hold time after the trigger is gone (longer for stalls, doubling on
repeats up to a cap), then work resumes with ONE worker until `ramp_tiles` thumbnails have completed."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class GuardLimits:
    stall_s: float = 6.0                  # no first byte / no body progress this long = a CDN stall
    seek_buffer_max_s: float = 20.0       # buffering after a seek/start-up that never ends is a stall after all
    hold_rebuffer_s: float = 20.0         # stay paused this long after the player is back to normal
    hold_stale_s: float = 10.0            # ... after telemetry is back
    stall_cooldown_s: float = 30.0        # first CDN-stall cooldown; doubles on each repeat
    stall_cooldown_max_s: float = 120.0
    calm_reset_s: float = 600.0           # no pause for this long forgets earlier stalls
    ramp_tiles: int = 10                  # thumbnails to complete with a single worker after a pause
    min_reports: int = 3                  # telemetry reports before a stream counts as established

    def scaled(self, k: float) -> "GuardLimits":
        """Same limits with every time scaled (tests run the real logic in a fraction of the time)."""
        return GuardLimits(self.stall_s * k, self.seek_buffer_max_s * k, self.hold_rebuffer_s * k, self.hold_stale_s * k,
                           self.stall_cooldown_s * k, self.stall_cooldown_max_s * k, self.calm_reset_s * k, self.ramp_tiles,
                           self.min_reports)


class PlaybackGuard:
    def __init__(self, governor, stall_seconds: Callable[[], float], clock: Callable[[], float] = time.monotonic,
                 limits: GuardLimits | None = None):
        self.g = governor
        self.stall_seconds = stall_seconds
        self.clock = clock
        self.l = limits or GuardLimits()
        self._lock = threading.Lock()
        self.reason: str | None = None            # why background work is held right now
        self._resume_at = 0.0
        self._stall_count = 0
        self._last_pause_end = -1e18
        self.ramp_left = 0
        self.pauses = {"rebuffer": 0, "stale": 0, "stall": 0}
        self.paused_total_s = 0.0
        self._paused_since: float | None = None
        self.last_reason: str | None = None

    # ------------------------------------------------------------------ triggers (read-only on the Governor)
    def _trigger(self, now: float) -> tuple[str | None, float]:
        """(reason, hold seconds) of the strongest trigger active right now."""
        st = self.g.guard_view()
        if st["rebuffering"] and (st["kind"] == "real" or (st["kind"] in ("seek", "startup") and
                                                           now - st["since"] > self.l.seek_buffer_max_s)):
            return "rebuffer", self.l.hold_rebuffer_s
        if st["reports"] >= self.l.min_reports and st["age"] is not None and st["age"] > self.g.l.telemetry_stale_s:
            return "stale", self.l.hold_stale_s
        stalled = self.stall_seconds()
        if stalled >= self.l.stall_s:
            return "stall", 0.0                    # hold decided below (backs off on repeats)
        return None, 0.0

    # ------------------------------------------------------------------ the decision workers ask for
    def check(self) -> str | None:
        """Reason background work must wait right now, or None. Cheap; call before claiming a task."""
        with self._lock:
            now = self.clock()
            reason, hold = self._trigger(now)
            if reason is not None:
                if reason == "stall":
                    if self.reason != "stall" and now - self._last_pause_end > self.l.calm_reset_s:
                        self._stall_count = 0
                    if self.reason != "stall":
                        self._stall_count += 1
                    hold = min(self.l.stall_cooldown_max_s, self.l.stall_cooldown_s * (2 ** (self._stall_count - 1)))
                if self.reason is None:
                    self.pauses[reason] += 1
                    self._paused_since = now
                    self.last_reason = reason
                elif reason != self.reason:
                    self.last_reason = reason
                self.reason = reason
                self._resume_at = max(self._resume_at, now + hold)
                return self.reason
            if self.reason is not None:
                if now >= self._resume_at:
                    if self._paused_since is not None:
                        self.paused_total_s += now - self._paused_since
                    self._paused_since = None
                    self.reason = None
                    self._last_pause_end = now
                    self.ramp_left = self.l.ramp_tiles
                    return None
                return self.reason
            return None

    def worker_slot_free(self, active_background: int) -> bool:
        """Recovery ramp: one background worker at a time until ramp_tiles thumbnails have completed."""
        with self._lock:
            return self.ramp_left <= 0 or active_background < 1

    def note_tile_done(self) -> None:
        with self._lock:
            if self.ramp_left > 0:
                self.ramp_left -= 1

    def stats(self) -> dict:
        with self._lock:
            now = self.clock()
            paused = self.paused_total_s + ((now - self._paused_since) if self._paused_since is not None else 0.0)
            return {"paused": self.reason, "last_reason": self.last_reason, "pauses": dict(self.pauses),
                    "paused_total_s": round(paused, 1), "ramp_left": self.ramp_left, "stall_s_now": round(self.stall_seconds(), 1),
                    "stall_count": self._stall_count}
