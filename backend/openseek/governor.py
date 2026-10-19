"""Playback-aware extraction governor + per-host connection ledger (pure logic, injectable clock).

Extraction yields to playback: AIMD on allowed concurrent range requests, driven by playback health
reported by the client and by our own latency/error observations. Hard ceilings are never exceeded and
a 429/Retry-After pauses all extraction against that host. Nothing here talks to a provider.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass
class Limits:
    start_conc: int = 1
    max_conc: int = 4                 # hard ceiling for extraction on one host (CDN host budget ~20, shared)
    host_budget: int = 12             # extraction + playback must stay under this per host
    max_rps: float = 12.0
    grow_after: int = 12              # healthy completions before +1 (additive increase)
    healthy_latency_s: float = 0.6
    buffer_ok_ms: int = 8000          # playback buffer depth considered safe
    resume_after_s: float = 8.0       # hysteresis: stay down this long after a bad signal
    telemetry_stale_s: float = 10.0   # no playback telemetry for this long => treat as unhealthy
    stable_reports: int = 10          # non-rebuffering reports (about 10 s) before a stall counts as a rebuffer
    telemetry_lost_s: float = 30.0    # ... for this long => the negotiation is over (caller falls back to Priority C)


class Governor:
    def __init__(self, limits: Limits | None = None, clock: Callable[[], float] = time.monotonic):
        self.l = limits or Limits()
        self.clock = clock
        self.conc = self.l.start_conc
        self._ok = 0
        self._lock = threading.Lock()
        self._bad_until = 0.0
        self._pause_until: dict[str, float] = {}      # host -> no extraction before this time
        self._host_inflight: dict[str, int] = {}
        self._playback_conn: dict[str, int] = {}      # host -> playback connections reported by the client
        self._telemetry_at: float | None = None
        self._buffer_ms: int | None = None
        self._rebuffering = False
        self._lat: deque = deque(maxlen=16)
        self.reports = 0                  # telemetry reports received
        self.min_buffer_ms: int | None = None
        self.rebuffer_events = 0          # stalls AFTER playback was established
        self.startup_buffering_events = 0  # initial loads: not stalls, never counted as one
        self.seek_buffering_events = 0     # buffering right after a scrub or skip: not a stall either
        self._stable = 0                  # consecutive non-rebuffering reports
        self.retry_after_events = 0
        self._rebuffer_kind: str | None = None     # "real" | "seek" | "startup" for the buffering in progress
        self._rebuffer_since = 0.0

    def guard_view(self) -> dict:
        """What the playback guard needs, in one consistent read."""
        with self._lock:
            now = self.clock()
            return {"rebuffering": self._rebuffering, "kind": self._rebuffer_kind, "since": self._rebuffer_since,
                    "reports": self.reports, "age": None if self._telemetry_at is None else now - self._telemetry_at}

    # ---- inputs
    def playback(self, buffer_ms: int | None, rebuffering: bool, host: str | None = None,
                 playback_conns: int = 1, seeking: bool = False) -> None:
        with self._lock:
            now = self.clock()
            if rebuffering and not self._rebuffering:
                self._rebuffer_since = now
                if seeking:
                    self.seek_buffering_events += 1      # the person scrubbed/skipped: buffering is expected, not a stall
                    self._rebuffer_kind = "seek"
                elif self._stable >= self.l.stable_reports:
                    self.rebuffer_events += 1            # a real stall in the middle of steady playback
                    self._rebuffer_kind = "real"
                else:
                    self.startup_buffering_events += 1   # the player was still loading or seeking
                    self._rebuffer_kind = "startup"
            elif not rebuffering:
                self._rebuffer_kind = None
            self._stable = 0 if rebuffering else self._stable + 1
            self.reports += 1
            if buffer_ms is not None:
                self.min_buffer_ms = buffer_ms if self.min_buffer_ms is None else min(self.min_buffer_ms, buffer_ms)
            self._telemetry_at, self._buffer_ms, self._rebuffering = now, buffer_ms, bool(rebuffering)
            if host:
                self._playback_conn[host] = max(0, int(playback_conns))
            if rebuffering or (buffer_ms is not None and buffer_ms < self.l.buffer_ok_ms // 2):
                self._down(now, hard=bool(rebuffering))

    def retry_after(self, host: str, seconds: float) -> None:
        with self._lock:
            now = self.clock()
            self.retry_after_events += 1
            self._pause_until[host] = max(self._pause_until.get(host, 0.0), now + max(1.0, float(seconds)))
            self._down(now, hard=True)

    def completed(self, latency_s: float, ok: bool) -> None:
        with self._lock:
            now = self.clock()
            if not ok:
                self._down(now, hard=False)
                return
            self._lat.append(latency_s)
            if latency_s > self.l.healthy_latency_s * 2:
                self._down(now, hard=False)
                return
            if now >= self._bad_until and self._playback_healthy(now) and latency_s <= self.l.healthy_latency_s:
                self._ok += 1
                if self._ok >= self.l.grow_after and self.conc < self.l.max_conc:
                    self.conc += 1
                    self._ok = 0

    # ---- decisions
    def status(self, host: str) -> str:
        """Why extraction against `host` may or may not start now:
        "ok" | "full" (concurrency/host budget) | "pause" (Retry-After) | "rebuffer" | "stale" | "lost" | "none"
        ("none" = no telemetry yet). "stale" waits; "lost" means telemetry is gone for good."""
        with self._lock:
            now = self.clock()
            if now < self._pause_until.get(host, 0.0):
                return "pause"
            if self._telemetry_at is None:
                return "none"
            age = now - self._telemetry_at
            if age > self.l.telemetry_lost_s:
                return "lost"
            if age > self.l.telemetry_stale_s:
                if self.conc > 1:
                    self.conc = 1
                return "stale"
            if self._rebuffering:
                return "rebuffer"
            if self.conc > 1 and not self._playback_healthy(now):
                self.conc = 1
            room = self.l.host_budget - self._playback_conn.get(host, 1)
            if max(0, min(self.conc, self.l.max_conc, room)) <= 0:
                return "full"
            return "ok"

    def allowed(self, host: str) -> int:
        """How many extraction requests may be in flight against `host` right now (0 unless healthy)."""
        if self.status(host) not in ("ok", "full"):
            return 0
        with self._lock:
            room = self.l.host_budget - self._playback_conn.get(host, 1)
            return max(0, min(self.conc, self.l.max_conc, room))

    def healthy(self, host: str | None = None) -> bool:
        """Fresh, non-rebuffering telemetry and (if given) no Retry-After pause on `host`."""
        with self._lock:
            now = self.clock()
            if host is not None and now < self._pause_until.get(host, 0.0):
                return False
            return self._playback_healthy(now)

    def stats(self) -> dict:
        with self._lock:
            age = None if self._telemetry_at is None else round(self.clock() - self._telemetry_at, 1)
            return {"conc": self.conc, "reports": self.reports, "telemetry_age_s": age,
                    "min_buffer_ms": self.min_buffer_ms, "rebuffer_events": self.rebuffer_events, "startup_buffering_events": self.startup_buffering_events, "seek_buffering_events": self.seek_buffering_events,
                    "rebuffering_now": self._rebuffering, "retry_after_events": self.retry_after_events}

    def telemetry_age(self) -> float | None:
        with self._lock:
            return None if self._telemetry_at is None else self.clock() - self._telemetry_at

    def try_acquire(self, host: str) -> bool:
        cap = self.allowed(host)
        with self._lock:
            n = self._host_inflight.get(host, 0)
            if n >= cap:
                return False
            self._host_inflight[host] = n + 1
            return True

    def release(self, host: str) -> None:
        with self._lock:
            self._host_inflight[host] = max(0, self._host_inflight.get(host, 1) - 1)

    def inflight(self, host: str) -> int:
        with self._lock:
            return self._host_inflight.get(host, 0)

    def rps_ceiling(self) -> float:
        return min(self.l.max_rps, float(self.conc) * 4.0)

    # ---- internals
    def _playback_healthy(self, now: float) -> bool:
        if self._telemetry_at is None or now - self._telemetry_at > self.l.telemetry_stale_s:
            return False                               # no signal => assume playback is at risk
        if self._rebuffering:
            return False
        return self._buffer_ms is None or self._buffer_ms >= self.l.buffer_ok_ms // 2

    def _down(self, now: float, hard: bool) -> None:
        self.conc = self.l.start_conc if hard else max(self.l.start_conc, self.conc // 2)
        self._ok = 0
        self._bad_until = now + self.l.resume_after_s
