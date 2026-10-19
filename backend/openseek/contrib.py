"""Guest contributions: a narrowly scoped way for an AUTHORISED guest device to hand its stream link to the owner's server so that
thumbnails for a title missing from the library are generated once and shared.

What makes this safe (see docs/security/CONTRIBUTIONS.md):
  * off by default, owner switch + a per-device Contributor permission (guests stay read-only otherwise);
  * a DEDICATED API (/v1/contribute*), never the privileged /v1/previews;
  * every hop of the fetch (initial URL and each redirect) goes through ContributionPolicy: https only, port 443, the host must be
    on the owner's allowlist of media/debrid hosts, then the normal SSRF policy (no private/loopback/metadata addresses, DNS pinned);
  * one source at a time globally, one job per device, its own byte bucket, background-only pacing, held while the owner's own
    playback guard is holding, and preempted the moment the owner needs the slot;
  * the link lives in memory only and is never logged (the log shows a salted hash of the host)."""
from __future__ import annotations

import re
import time
from collections import deque
from urllib.parse import urlsplit

from .netpolicy import Denied

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
DEFAULT_HOSTS = ("torbox.app", "tb-cdn.io", "real-debrid.com", "debrid.link", "alldebrid.com", "premiumize.me")


def clean_hosts(items) -> list[str]:
    out: list[str] = []
    for h in items:
        h = str(h).strip().lower().lstrip(".")
        if h and _HOST.fullmatch(h) and h not in out and len(out) < 64:
            out.append(h)
    return out


class ContributionPolicy:
    """The owner's normal DestinationPolicy plus the extra rules for guest-supplied links."""

    def __init__(self, base, hosts: list[str], https_only: bool = True, on_denied=None):
        self.base = base
        self.hosts = list(hosts)
        self.https_only = https_only
        self.on_denied = on_denied

    def _host_ok(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def vet(self, url: str):
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        try:
            if self.https_only and p.scheme.lower() != "https":
                raise Denied("contrib_scheme")
            if p.username or p.password:
                raise Denied("contrib_userinfo")
            if self.https_only and p.port not in (None, 443):
                raise Denied("contrib_port")
            if not host or not self._host_ok(host):
                raise Denied("contrib_host")
        except Denied:
            if self.on_denied is not None and host and _HOST.fullmatch(host):
                self.on_denied(host)              # the HOST NAME only (never the path or query) so the owner can decide to allow it
            raise
        return self.base.vet(url)

    def __getattr__(self, name):
        return getattr(self.base, name)


class Spacing:
    """At most `n` events per `window` seconds per key (device)."""

    def __init__(self, n: int, window: float = 60.0, clock=time.monotonic):
        self.n, self.window, self.clock = n, window, clock
        self.ev: dict[str, deque] = {}

    def hit(self, key: str) -> bool:
        now = self.clock()
        d = self.ev.setdefault(key, deque())
        while d and now - d[0] > self.window:
            d.popleft()
        if len(d) >= self.n:
            return False
        d.append(now)
        return True
