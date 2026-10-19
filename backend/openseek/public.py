"""The internet-facing guest listener (OPENSEEK_PUBLIC_PORT, off by default).

Deny by default. A request is classified by exact (method, path) BEFORE anything else happens; an unlisted
combination is a uniform 404 and counts as a strike against the sender. The client address is the TCP peer: no
forwarding header is ever consulted (uvicorn runs with proxy_headers=False), so a spoofed X-Forwarded-For changes
nothing. Rates, strikes and blocks are per client address."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

_ID32 = r"[0-9a-f]{32}"
_CAP = r"[A-Za-z0-9_-]{16,64}"
_NAME = r"[0-9A-Za-z_.-]{1,40}"

# what needs no token: the entrance (health, invitation pairing) and the borrowed-content URLs, whose HMAC capability
# binds device + preview + expiry (and which the data plane re-checks for guest role, revocation and the kill switch)
OPEN_ROUTES = (
    ("health", "GET", re.compile(r"/v1/health")),
    ("pair", "POST", re.compile(r"/v1/pair/start")),
    ("pair", "POST", re.compile(r"/v1/pair/claim")),
    ("cap", "GET", re.compile(rf"/v1/c/{_CAP}/{_ID32}/thumbs\.vtt")),
    ("cap", "GET", re.compile(rf"/v1/c/{_CAP}/{_ID32}/tiles/{_NAME}")),
    ("cap", "GET", re.compile(rf"/v1/c/{_CAP}/{_ID32}/sheets/{_NAME}")),
)


def classify(method: str, path: str, guest_allowed: Callable[[str, str], bool]) -> str | None:
    """'health' | 'pair' | 'cap' | 'api' (authenticated guest routes) | None (deny)."""
    for kind, m, rx in OPEN_ROUTES:
        if m == method and rx.fullmatch(path):
            return kind
    return "api" if guest_allowed(method, path) else None


@dataclass
class PublicLimits:
    per_minute: dict
    strikes: int = 20                 # refused/unknown/unauthorised requests ...
    strike_window_s: float = 600.0    # ... within this window ...
    block_s: float = 900.0            # ... block the address for this long
    max_tracked: int = 5000


def default_limits() -> PublicLimits:
    return PublicLimits(per_minute={"health": 30, "pair": 12, "api": 300, "cap": 900, "unknown": 30})


class PublicLimiter:
    def __init__(self, limits: PublicLimits | None = None, clock: Callable[[], float] = time.monotonic):
        self.l = limits or default_limits()
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque] = {}
        self._strikes: dict[str, deque] = {}
        self._blocked: dict[str, float] = {}
        self.refused_total = 0

    def _prune(self, now: float) -> None:
        if len(self._hits) + len(self._strikes) <= self.l.max_tracked:
            return
        self._hits = {k: d for k, d in self._hits.items() if d and now - d[-1] < 60}
        self._strikes = {k: d for k, d in self._strikes.items() if d and now - d[-1] < self.l.strike_window_s}
        self._blocked = {k: t for k, t in self._blocked.items() if t > now}

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return self._blocked.get(ip, 0) > self.clock()

    def allow(self, ip: str, kind: str) -> bool:
        """Count one request of `kind`; False = over the per-minute budget (HTTP 429)."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            d = self._hits.setdefault((ip, kind), deque())
            while d and now - d[0] > 60:
                d.popleft()
            if len(d) >= self.l.per_minute.get(kind, 30):
                self.refused_total += 1
                return False
            d.append(now)
            return True

    def strike(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            d = self._strikes.setdefault(ip, deque())
            d.append(now)
            while d and now - d[0] > self.l.strike_window_s:
                d.popleft()
            if len(d) >= self.l.strikes:
                self._blocked[ip] = now + self.l.block_s
                d.clear()

    def clear(self) -> None:
        with self._lock:
            self._hits.clear(); self._strikes.clear(); self._blocked.clear()

    def snapshot(self) -> dict:
        with self._lock:
            now = self.clock()
            return {"tracked": len(self._hits), "blocked": sum(1 for t in self._blocked.values() if t > now),
                    "refused_total": self.refused_total}
