"""Vetted upstream fetcher and the loopback range proxy (SR-8..SR-11, SR-14).

* `Upstream` talks to the media origin. Every hop is vetted by `DestinationPolicy`; redirects
  are followed here (<= 5, re-vetted, no https->http downgrade, forwarded headers dropped once
  the origin changes). The signed redirect is resolved ONCE by the preflight; later reads reuse
  the final URL over one keep-alive connection. Only bounded single ranges are ever requested.
* `Budget` enforces the per-job byte budget *before* each request, so the bytes fetched never
  exceed it. `TokenBucket` is the GLOBAL background (P2) rate limit.
* `RangeProxy` listens on 127.0.0.1 only. The extraction worker reads
  `http://127.0.0.1:<port>/j/<128-bit cap>`; unknown caps get 404; it never sees the real URL.
"""
from __future__ import annotations

import datetime
import email.utils
import hashlib
import http.client
import logging
import re
import secrets
import socket
import ssl
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urljoin

from . import __version__, mediasniff
from .logutil import host_tag
from .netpolicy import Denied, DestinationPolicy, Target

log = logging.getLogger("openseek.fetch")

PROBE_BYTES = 256 * 1024          # preflight Range bytes=0-262143
FP_BYTES = 64 * 1024              # SR-14 head/tail sample
MAX_REDIRECTS = 5
MAX_SOURCE_SIZE = 1 << 50          # a Content-Range total above this (1 PiB) is hostile or broken; fingerprint() packs the size in 8 bytes
MAX_PROXY_RANGE = 8 * 1024 * 1024
CHUNK = 64 * 1024
SLOW_GRACE_S = 30.0               # network time any single range request may take regardless of its size ...
MIN_RATE_BPS = 16 * 1024          # ... plus length / this rate: a slower source is abandoned (slow-drip origins must not pin a thread)
REDIRECT_CODES = (301, 302, 303, 307, 308)
DEFAULT_UA = f"OpenSeek/{__version__}"
_CR_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class UpstreamError(Exception):
    """`code` is a contract error code; `reason` is internal detail (never sent to clients)."""

    retry_after: float | None = None

    def __init__(self, code: str, reason: str = ""):
        super().__init__(f"{code}:{reason}")
        self.code = code
        self.reason = reason


def status_error(status: int) -> UpstreamError:
    if status in (401, 403, 410):
        return UpstreamError("source_expired", f"http_{status}")
    if status == 200:
        return UpstreamError("range_unsupported", "http_200")
    if status == 404:
        return UpstreamError("source_unreachable", "http_404")
    return UpstreamError("source_unreachable", f"http_{status}")


RETRY_AFTER_MAX_S = 3600.0
RETRY_AFTER_DEFAULT_429_S = 30.0


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header: delta-seconds ("120", "0") or an HTTP-date (RFC 9110: IMF-fixdate,
    obsolete RFC 850 and asctime forms). Dates are measured against `now` (epoch seconds; default the wall clock), a
    date in the past is 0, anything unparseable is None, and the result is clamped to [0, RETRY_AFTER_MAX_S]."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v.isdigit():                                       # delta-seconds is a non-negative integer
        return min(float(int(v)), RETRY_AFTER_MAX_S)
    try:
        when = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:                               # asctime() carries no zone: HTTP dates are GMT
        when = when.replace(tzinfo=datetime.timezone.utc)
    t = time.time() if now is None else now
    return max(0.0, min(when.timestamp() - t, RETRY_AFTER_MAX_S))


def rate_limit_error(status: int, header_value: str | None) -> "UpstreamError":
    """status_error plus the provider's back-off. A 429 always carries one (default 30 s when the header is
    missing or unreadable); a 503 only when it sent a usable Retry-After. Callers must never retry before it."""
    err = status_error(status)
    ra = parse_retry_after(header_value)
    if ra is None and status == 429:
        ra = RETRY_AFTER_DEFAULT_429_S
    if status in (429, 503) and ra is not None:
        err.retry_after = ra
    return err


class UpstreamStats:
    """Per-source request accounting for the benchmark and the admin metrics (no URLs, no headers)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = 0
        self.by_status: dict[str, int] = {}
        self.resolutions = 0          # times the ORIGINAL url was requested (addon link -> provider resolve + redirect)
        self.redirect_hops = 0
        self.rate_limited = 0         # 429, or 503 with a Retry-After
        self.retry_after_max_s = 0.0
        self.inflight = 0
        self.max_inflight = 0
        self._lat: deque = deque(maxlen=2048)
        # body phase (after the headers): what the latency above does not cover
        self.body_requests = 0
        self.body_bytes = 0
        self.body_read_s = 0.0        # waiting on the network inside resp.read()
        self.body_gate_s = 0.0        # byte pacing (our own throttle) before each read
        self.body_sink_s = 0.0        # handing bytes to the loopback client
        self._body: deque = deque(maxlen=2048)     # (bytes, read_s) per range request
        # stall detection: when did each request in flight start, and when did a body last move
        self._starts: dict[int, float] = {}
        self._seq = 0
        self.body_active = 0
        self.body_progress_at = 0.0

    def stall_seconds(self, now: float | None = None) -> float:
        """The longest an in-flight request has waited for its first byte, or a body in progress has not moved."""
        now = time.monotonic() if now is None else now
        with self._lock:
            wait = max((now - t for t in self._starts.values()), default=0.0)
            body = (now - self.body_progress_at) if self.body_active else 0.0
        return max(wait, body)

    def body_begin(self) -> None:
        with self._lock:
            self.body_active += 1
            self.body_progress_at = time.monotonic()

    def body_progress(self) -> None:
        self.body_progress_at = time.monotonic()

    def body_end(self) -> None:
        with self._lock:
            self.body_active = max(0, self.body_active - 1)

    def note_body(self, nbytes: int, read_s: float, gate_s: float, sink_s: float) -> None:
        with self._lock:
            self.body_requests += 1
            self.body_bytes += nbytes
            self.body_read_s += read_s
            self.body_gate_s += gate_s
            self.body_sink_s += sink_s
            self._body.append((nbytes, read_s))

    def begin(self) -> int:
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self._seq += 1
            self._starts[self._seq] = time.monotonic()
            return self._seq

    def end(self, status: int | None, latency_s: float, token: int | None = None) -> None:
        with self._lock:
            if token is not None:
                self._starts.pop(token, None)
            elif self._starts:
                self._starts.pop(min(self._starts), None)
            self.inflight = max(0, self.inflight - 1)
            self.requests += 1
            key = str(status) if status is not None else "error"
            self.by_status[key] = self.by_status.get(key, 0) + 1
            self._lat.append(latency_s)

    def note_resolution(self) -> None:
        with self._lock:
            self.resolutions += 1

    def note_redirect(self) -> None:
        with self._lock:
            self.redirect_hops += 1

    def note_rate_limit(self, retry_after_s: float | None) -> None:
        with self._lock:
            self.rate_limited += 1
            if retry_after_s:
                self.retry_after_max_s = max(self.retry_after_max_s, retry_after_s)

    def snapshot(self) -> dict:
        with self._lock:
            lat = sorted(self._lat)

            def pct(q: float) -> float | None:
                return round(lat[min(len(lat) - 1, int(q * len(lat)))] * 1000, 1) if lat else None
            return {"requests": self.requests, "by_status": dict(self.by_status), "resolutions": self.resolutions,
                    "redirect_hops": self.redirect_hops, "rate_limited": self.rate_limited,
                    "retry_after_max_s": round(self.retry_after_max_s, 1), "inflight": self.inflight,
                    "max_inflight": self.max_inflight, "latency_ms_p50": pct(0.5), "latency_ms_p95": pct(0.95),
                    "latency_ms_max": round(lat[-1] * 1000, 1) if lat else None,
                    "body": self._body_snapshot()}

    def _body_snapshot(self) -> dict:
        b = sorted(s for _, s in self._body)
        rates = sorted(n / s for n, s in self._body if s > 0)
        q = lambda xs, f: round(xs[min(len(xs) - 1, int(f * len(xs)))], 4) if xs else None  # noqa: E731
        return {"requests": self.body_requests, "bytes": self.body_bytes, "read_s": round(self.body_read_s, 2),
                "gate_s": round(self.body_gate_s, 2), "sink_s": round(self.body_sink_s, 2),
                "read_ms_p50": None if not b else round(q(b, 0.5) * 1000, 1), "read_ms_p95": None if not b else round(q(b, 0.95) * 1000, 1),
                "read_ms_max": None if not b else round(b[-1] * 1000, 1),
                "MBps_p50": None if not rates else round(q(rates, 0.5) / 1e6, 2), "MBps_p5": None if not rates else round(q(rates, 0.05) / 1e6, 2),
                "MBps_mean": round(self.body_bytes / self.body_read_s / 1e6, 2) if self.body_read_s > 0 else None}


def fingerprint(size: int, head64: bytes, tail64: bytes) -> str:
    """SR-14: SHA-256("v1" || u64be(size) || SHA-256(first 64 KiB) || SHA-256(last 64 KiB))."""
    h = hashlib.sha256()
    h.update(b"v1")
    h.update(size.to_bytes(8, "big"))
    h.update(hashlib.sha256(head64).digest())
    h.update(hashlib.sha256(tail64).digest())
    return h.hexdigest()


class Budget:
    def __init__(self, limit: int, request_bucket=None, urgent: Callable[[], bool] | None = None):
        self.limit = int(limit)
        self.used = 0
        self.requests = 0
        self.exceeded = False
        self.request_bucket = request_bucket
        self.urgent = urgent
        self._lock = threading.Lock()

    def reserve(self, n: int) -> None:
        with self._lock:
            if self.used + n > self.limit:
                self.exceeded = True
                raise UpstreamError("budget_exceeded", "budget")

    def add(self, n: int) -> None:
        with self._lock:
            self.used += n

    def add_request(self) -> None:
        with self._lock:
            self.requests += 1


class TokenBucket:
    """Global pacer for background traffic (bytes/s with a small burst)."""

    def __init__(self, rate_bytes_per_s: float, burst: int = 256 * 1024, clock=time.monotonic,
                 sleep=time.sleep):
        self.rate = float(rate_bytes_per_s)
        self.burst = burst
        self._clock = clock
        self._sleep = sleep
        self._next_free = 0.0
        self._last_urgent = 0.0
        self._lock = threading.Lock()

    def throttle(self, n: int, active: Callable[[], bool],
                 urgent: Callable[[], bool] | None = None) -> None:
        """Account n bytes; sleep while `active()` stays true (re-checked every 50 ms, so a
        frame upgraded to foreground stops waiting)."""
        if not active():
            return
        is_urgent = bool(urgent and urgent())
        with self._lock:
            now = self._clock()
            start = max(self._next_free, now - self.burst / self.rate)
            self._next_free = start + n / self.rate
            if is_urgent:
                due = min(self._next_free, max(now, self._last_urgent + 0.12))
                self._last_urgent = due
            else:
                due = self._next_free
        while active():
            if not is_urgent and urgent is not None and urgent():
                return
            delay = due - self._clock()
            if delay <= 0:
                return
            self._sleep(min(delay, 0.05))


@dataclass
class Preflight:
    size: int
    head: bytes                      # bytes [0, len(head))
    tail_offset: int
    tail: bytes                      # bytes [tail_offset, size)
    fp: str
    fmt: str | None
    has_index: bool | None
    final_host_tag: str
    redirects: int


def _parse_cr(value: str | None) -> tuple[int, int, int] | None:
    m = _CR_RE.match((value or "").strip())
    if not m:
        return None
    a, b, total = (int(x) for x in m.groups())
    if b < a or b >= total or total > MAX_SOURCE_SIZE:
        return None
    return a, b, total


class Upstream:
    """One media source: vetted, redirect-resolving, keep-alive, bounded-range reader."""

    def __init__(self, policy: DestinationPolicy, url: str, headers: dict[str, str], budget: Budget,
                 timeout: float = 15.0):
        self.policy = policy
        self.original_url = url
        self.original_headers = dict(headers)
        self.budget = budget
        self.timeout = timeout
        self.final: Target | None = None
        self.final_headers: dict[str, str] = {}
        self.size: int | None = None
        self.redirected = False
        self.reresolves = 0
        self.redirects = 0
        self.stats = UpstreamStats()
        self._tls = threading.local()
        self._pool: list[tuple[tuple, http.client.HTTPConnection]] = []
        self._active_conns: set[http.client.HTTPConnection] = set()
        self._lock = threading.RLock()
        self.closed = False

    # ---------------------------------------------------------------- low level
    def _close_raw_conn(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._active_conns.discard(conn)
        try:
            if conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _drop_conn(self) -> None:
        conn = getattr(self._tls, "conn", None)
        self._tls.conn = None
        self._tls.origin = None
        if conn is not None:
            self._close_raw_conn(conn)

    def _stash_conn(self) -> None:
        """Hand the current thread's keep-alive connection to the shared pool (used after preflight)."""
        conn = getattr(self._tls, "conn", None)
        origin = getattr(self._tls, "origin", None)
        self._tls.conn = None
        self._tls.origin = None
        if conn is not None and origin is not None:
            with self._lock:
                if self.closed:
                    pass
                else:
                    self._pool.append((origin, conn))
                    return
            self._close_raw_conn(conn)

    def _conn_for(self, target: Target) -> http.client.HTTPConnection:
        if self.closed:
            raise UpstreamError("source_unreachable", "closed")
        origin_key = (target.origin, target.addrs)
        conn = getattr(self._tls, "conn", None)
        if conn is not None and getattr(self._tls, "origin", None) == origin_key:
            return conn
        self._drop_conn()
        with self._lock:
            for idx, (pooled_origin, pooled_conn) in enumerate(self._pool):
                if pooled_origin == origin_key:
                    self._pool.pop(idx)
                    self._tls.conn = pooled_conn
                    self._tls.origin = origin_key
                    return pooled_conn
        try:
            conn = self.policy.connect(target, self.timeout)
        except ssl.SSLError as exc:
            self.last_tls_host = target.host
            log.warning("upstream TLS failed type=%s verify_code=%s",
                        type(exc).__name__, getattr(exc, "verify_code", None))
            raise UpstreamError("source_unreachable", "tls") from None
        except OSError:
            raise UpstreamError("source_unreachable", "connect") from None
        with self._lock:
            if self.closed:
                self._close_raw_conn(conn)
                raise UpstreamError("source_unreachable", "closed")
            self._active_conns.add(conn)
        self._tls.conn = conn
        self._tls.origin = origin_key
        return conn

    def _send(self, target: Target, fwd: dict[str, str], start: int, end: int,
              range_header: str | None = None) -> http.client.HTTPResponse:
        """GET one bounded range; reconnects once if a kept-alive connection went stale."""
        for attempt in range(2):
            if self.budget.request_bucket is not None:
                self.budget.request_bucket.throttle(1, lambda: True, urgent=self.budget.urgent)
            conn = self._conn_for(target)
            t_req = time.perf_counter()
            tok = self.stats.begin()
            ended = False
            try:
                conn.putrequest("GET", target.path, skip_accept_encoding=True)
                conn.putheader("Range", range_header or f"bytes={start}-{end}")
                conn.putheader("Accept-Encoding", "identity")
                conn.putheader("User-Agent", fwd.get("User-Agent", DEFAULT_UA))
                for k in ("Referer", "Origin"):
                    if k in fwd:
                        conn.putheader(k, fwd[k])
                conn.endheaders()
                resp = conn.getresponse()
                self.budget.add_request()
                self.stats.end(resp.status, time.perf_counter() - t_req, tok)
                ended = True
                return resp
            except ssl.SSLError as exc:
                self.last_tls_host = target.host
                log.warning("upstream TLS failed type=%s verify_code=%s",
                            type(exc).__name__, getattr(exc, "verify_code", None))
                self._drop_conn()
                raise UpstreamError("source_unreachable", "tls") from None
            except (http.client.HTTPException, OSError):
                self._drop_conn()
                if attempt:
                    raise UpstreamError("source_unreachable", "io") from None
            finally:
                if not ended:
                    self.stats.end(None, time.perf_counter() - t_req, tok)
        raise UpstreamError("source_unreachable", "io")

    def _discard(self, resp: http.client.HTTPResponse, limit: int = 64 * 1024) -> None:
        """Read a small (redirect/error) body to keep the connection; drop it if large/unknown."""
        length = resp.getheader("Content-Length")
        try:
            if length is not None and length.isdigit() and int(length) <= limit:
                data = resp.read(int(length))
                self.budget.add(len(data))
                return
        except (http.client.HTTPException, OSError):
            pass
        resp.close()
        self._drop_conn()

    def _open(self, url: str, headers: dict[str, str], start: int, end: int):
        """Follow redirects (vetting every hop). Returns (target, fwd_headers, response)."""
        fwd = dict(headers)
        prev: Target | None = None
        self.stats.note_resolution()          # the addon/redirect hop may cost the provider one link request
        for hop in range(MAX_REDIRECTS + 1):
            try:
                target = self.policy.vet(url)
            except Denied as e:
                raise UpstreamError("source_forbidden", e.reason) from None
            if prev is not None:
                if prev.scheme == "https" and target.scheme == "http":
                    raise UpstreamError("source_forbidden", "downgrade")
                if target.origin != prev.origin:
                    fwd = {}  # forwarded headers never cross an origin change
            resp = self._send(target, fwd, start, end)
            if resp.status in REDIRECT_CODES:
                loc = resp.getheader("Location")
                self._discard(resp)
                if not loc:
                    raise UpstreamError("source_unreachable", "redirect_without_location")
                self.redirects += 1
                self.stats.note_redirect()
                self.redirected = True
                url = urljoin(target.url(), loc.strip())
                prev = target
                continue
            return target, fwd, resp
        raise UpstreamError("source_unreachable", "too_many_redirects")

    def _read_body(self, resp: http.client.HTTPResponse, length: int, sink: Callable[[bytes], None],
                   gate: Callable[[int], None] | None) -> int:
        got = 0
        read_s = gate_s = sink_s = 0.0
        pc = time.perf_counter
        self.stats.body_begin()
        try:
            while got < length:
                n = min(CHUNK, length - got)
                if gate:
                    t = pc()
                    gate(n)
                    gate_s += pc() - t
                t = pc()
                data = resp.read1(n)       # one network read: read(n) would loop until n bytes arrive and never return to the deadline check
                read_s += pc() - t
                if not data:
                    break
                if read_s > SLOW_GRACE_S + length / MIN_RATE_BPS:
                    self._drop_conn()      # too slow overall (slow-drip): treated like a dropped connection
                    got += len(data)
                    self.budget.add(len(data))
                    sink(data)
                    break
                self.stats.body_progress()
                self.budget.add(len(data))
                got += len(data)
                t = pc()
                sink(data)
                sink_s += pc() - t
        except (http.client.HTTPException, OSError):
            self._drop_conn()
        finally:
            self.stats.body_end()
        self.stats.note_body(got, read_s, gate_s, sink_s)
        return got

    # ---------------------------------------------------------------- preflight
    def preflight(self) -> Preflight:
        with self._lock:
            self.budget.reserve(PROBE_BYTES)
            target, fwd, resp = self._open(self.original_url, self.original_headers, 0, PROBE_BYTES - 1)
            if resp.status != 206:
                err = rate_limit_error(resp.status, resp.getheader("Retry-After"))
                if err.retry_after is not None:
                    self.stats.note_rate_limit(err.retry_after)
                resp.close()
                self._drop_conn()   # never drain a 200 (it would be the whole file)
                raise err
            cr = _parse_cr(resp.getheader("Content-Range"))
            if cr is None or cr[0] != 0:
                resp.close()
                self._drop_conn()
                raise UpstreamError("range_unsupported", "content_range")
            _, b, total = cr
            want = min(b + 1, PROBE_BYTES)
            buf = bytearray()
            got = self._read_body(resp, want, buf.extend, None)
            if b + 1 > PROBE_BYTES or got < want:
                resp.close()
                self._drop_conn()   # server sent more than asked (or dropped): do not reuse
            if got < want:
                raise UpstreamError("source_unreachable", "short_read")
            head = bytes(buf)
            self.final, self.final_headers, self.size = target, fwd, total

            if total <= len(head):
                tail_off, tail = 0, head
            else:
                tail_off = max(len(head), total - FP_BYTES)
                tail = self.read_bytes(tail_off, total - 1)
            # SR-14 fingerprint over the first and last 64 KiB
            last64 = (head + tail)[-FP_BYTES:] if tail_off <= len(head) else tail[-FP_BYTES:]
            fp = fingerprint(total, head[:FP_BYTES], last64)
            fmt = mediasniff.sniff(head)
            has_index = None
            if fmt == "matroska":
                def read_at(off: int, length: int) -> bytes:
                    end = off + length - 1
                    if end < len(head):
                        return head[off:end + 1]
                    if off >= tail_off:
                        return tail[off - tail_off:end - tail_off + 1]
                    return self.read_bytes(off, min(end, total - 1))
                has_index = mediasniff.mkv_has_index(head, total, read_at)
            self._stash_conn()
            log.info("preflight ok %s size=%d fmt=%s index=%s redirects=%d", host_tag(target.host), total,
                     fmt, has_index, self.redirects)
            return Preflight(total, head, tail_off, tail, fp, fmt, has_index, host_tag(target.host),
                             self.redirects)

    # ---------------------------------------------------------------- ranged reads
    def read_bytes(self, start: int, end: int) -> bytes:
        buf = bytearray()
        self.read_range(start, end, buf.extend, None)
        return bytes(buf)

    def read_range(self, start: int, end: int, sink: Callable[[bytes], None],
                   gate: Callable[[int], None] | None) -> None:
        """Deliver exactly bytes [start, end] to sink (resuming after drops) or raise UpstreamError."""
        with self._lock:
            target, fwd, total_size = self.final, self.final_headers, self.size
        if target is None or total_size is None:
            raise UpstreamError("internal", "no_preflight")
        if not 0 <= start <= end < total_size:
            raise UpstreamError("internal", "bad_range")
        self.budget.reserve(end - start + 1)
        pos, failures = start, 0
        while pos <= end:
            with self._lock:
                target, fwd = self.final, self.final_headers
            resp = self._send(target, fwd, pos, end)
            if resp.status in (401, 403, 410) and self.redirected:
                with self._lock:
                    if self.reresolves < 2:
                        # signed CDN link expired: resolve the original (addon) URL once more
                        self._discard(resp)
                        self.reresolves += 1
                        new_target, new_fwd, resp = self._open(self.original_url, self.original_headers, pos, end)
                        if resp.status == 206:
                            cr = _parse_cr(resp.getheader("Content-Range"))
                            if cr is None or cr[2] != total_size:
                                resp.close()
                                self._drop_conn()
                                raise UpstreamError("source_expired", "content_changed")
                            self.final, self.final_headers = new_target, new_fwd
            if resp.status != 206:
                if resp.status == 200:
                    resp.close()
                    self._drop_conn()
                else:
                    err = rate_limit_error(resp.status, resp.getheader("Retry-After"))
                    self._discard(resp)
                    if err.retry_after is not None:
                        self.stats.note_rate_limit(err.retry_after)   # honoured by the governor, never retried here
                    raise err
                raise status_error(resp.status)
            cr = _parse_cr(resp.getheader("Content-Range"))
            if cr is None or cr[0] != pos or cr[2] != total_size:
                resp.close()
                self._drop_conn()
                raise UpstreamError("source_unreachable", "content_range_mismatch")
            want = min(cr[1], end) - pos + 1
            got = self._read_body(resp, want, sink, gate)
            if cr[1] > end:
                resp.close()
                self._drop_conn()
            pos += got
            if got < want:
                failures += 1
                self._drop_conn()
                if failures > 2:
                    raise UpstreamError("source_unreachable", "dropped")

    def read_multi(self, ranges: list[tuple[int, int]], on_part: Callable[[int, int, bytes], None],
                   gate: Callable[[int], None] | None = None, cancelled: Callable[[], bool] = lambda: False) -> int:
        """ONE request for several byte ranges (multipart/byteranges). on_part(start, end, data) is called as each
        validated part completes, so the first window is usable before the last has arrived. Returns the parts
        delivered. Never retries: a malformed or unsupported answer raises MultiRangeError and the caller falls back to
        single-range reads (the pooled connection is dropped, so a half-read body can never be reused)."""
        with self._lock:
            target, fwd, total_size = self.final, self.final_headers, self.size
        if target is None or total_size is None:
            raise UpstreamError("internal", "no_preflight")
        want_bytes = sum(e - s + 1 for s, e in ranges)
        if (not ranges or any(not 0 <= s <= e < total_size for s, e in ranges)
                or sorted(ranges) != sorted(set(ranges))):
            raise UpstreamError("internal", "bad_range")
        self.budget.reserve(want_bytes)
        resp = self._send(target, fwd, 0, 0, range_header="bytes=" + ",".join(f"{s}-{e}" for s, e in ranges))
        ok = False
        try:
            if resp.status != 206:
                if resp.status == 200:
                    raise MultiRangeUnsupported("whole-file answer")
                err = rate_limit_error(resp.status, resp.getheader("Retry-After"))
                if err.retry_after is not None:
                    self.stats.note_rate_limit(err.retry_after)
                raise err
            ctype = resp.getheader("Content-Type") or ""
            m = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
            if not ctype.lower().startswith("multipart/byteranges") or m is None:
                raise MultiRangeUnsupported("not multipart/byteranges")
            parser = ByteRangesParser(m.group(1), ranges, total_size)
            cap = want_bytes + 1024 * (len(ranges) + 1)
            got = parts = 0
            read_s = gate_s = 0.0
            pc = time.perf_counter
            self.stats.body_begin()
            body_open = True
            while not parser.done:
                if cancelled():
                    raise MultiRangeError("cancelled")
                if gate:
                    t = pc()
                    gate(CHUNK)
                    gate_s += pc() - t
                t = pc()
                data = resp.read(CHUNK)
                read_s += pc() - t
                if not data:
                    break
                self.stats.body_progress()
                got += len(data)
                if got > cap:
                    raise MultiRangeError("answer larger than asked for")
                self.budget.add(len(data))
                for s0, e0, payload in parser.feed(data):
                    parts += 1
                    on_part(s0, e0, payload)
            self.stats.body_end()
            body_open = False
            self.stats.note_body(got, read_s, gate_s, 0.0)
            if parts != len(ranges):
                raise MultiRangeError(f"{parts} parts for {len(ranges)} ranges")
            ok = True
            return parts
        finally:
            if not ok:
                if locals().get("body_open"):
                    self.stats.body_end()
                try:
                    resp.close()
                finally:
                    self._drop_conn()                      # never reuse a connection with an unread multipart body

    def drop_connection(self) -> None:
        """Abort active upstream connections from another thread (unblocks pending reads).
        The next request reconnects to the vetted address."""
        with self._lock:
            conns = list(self._active_conns)
        for c in conns:
            if c.sock is not None:
                try:
                    c.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self) -> None:
        self.closed = True
        self.drop_connection()
        self._drop_conn()
        with self._lock:
            conns = list(self._active_conns) + [c for _, c in self._pool]
            self._active_conns.clear()
            self._pool.clear()
        for c in conns:
            self._close_raw_conn(c)


# ===================================================================== multi-range
class MultiRangeError(Exception):
    """The multi-range answer was not what was asked for (parts already delivered stay valid)."""


class MultiRangeUnsupported(MultiRangeError):
    """The server does not do multipart/byteranges for this request: do not ask again."""


_CR = re.compile(rb"(?i)content-range:\s*bytes\s+(\d+)-(\d+)/(\d+)")


class ByteRangesParser:
    """Incremental multipart/byteranges parser. feed() returns the parts completed by the new bytes as
    (start, end, payload); every part is checked against the ranges that were asked for."""

    def __init__(self, boundary: str, expected: list[tuple[int, int]], total: int):
        self.delim = b"--" + boundary.encode()
        self.expected = set(expected)
        self.total = total
        self.buf = bytearray()
        self.state = "preamble"
        self.cur: tuple[int, int] | None = None
        self.seen: set[tuple[int, int]] = set()
        self.done = False

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        self.buf.extend(data)
        out: list[tuple[int, int, bytes]] = []
        while True:
            if self.state == "preamble":
                i = self.buf.find(self.delim)
                if i < 0:
                    if len(self.buf) > 4096:
                        raise MultiRangeError("no boundary")
                    return out
                del self.buf[:i + len(self.delim)]
                self.state = "after_boundary"
            if self.state == "after_boundary":
                if len(self.buf) < 2:
                    return out
                if self.buf[:2] == b"--":
                    self.done = True
                    return out
                if self.buf[:2] != b"\r\n":
                    raise MultiRangeError("bad boundary line")
                del self.buf[:2]
                self.state = "headers"
            if self.state == "headers":
                i = self.buf.find(b"\r\n\r\n")
                if i < 0:
                    if len(self.buf) > 8192:
                        raise MultiRangeError("headers too long")
                    return out
                m = _CR.search(bytes(self.buf[:i]))
                if m is None:
                    raise MultiRangeError("part without Content-Range")
                rng = (int(m.group(1)), int(m.group(2)))
                if int(m.group(3)) != self.total:
                    raise MultiRangeError("total size changed")
                if rng not in self.expected or rng in self.seen:
                    raise MultiRangeError("unexpected or repeated part")
                del self.buf[:i + 4]
                self.cur = rng
                self.state = "body"
            if self.state == "body":
                assert self.cur is not None
                n = self.cur[1] - self.cur[0] + 1
                if len(self.buf) < n + 2:
                    return out
                payload = bytes(self.buf[:n])
                if self.buf[n:n + 2] != b"\r\n":
                    raise MultiRangeError("part length mismatch")
                del self.buf[:n + 2]
                self.seen.add(self.cur)
                out.append((self.cur[0], self.cur[1], payload))
                self.cur = None
                self.state = "preamble"


# ===================================================================== proxy
@dataclass
class ProxySource:
    """What the proxy needs for one job: upstream + cached preflight bytes + throttle state."""

    upstream: Upstream
    size: int
    chunks: list[tuple[int, bytes]] = field(default_factory=list)
    background: Callable[[], bool] = lambda: False      # True while the in-flight frame is P2
    bucket: TokenBucket | None = None
    shared_cache: bool = False
    last_error: UpstreamError | None = None
    proxied_requests: int = 0
    throttle_seconds: float = 0.0
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)
    _range_cache: OrderedDict[tuple[int, int], bytes] = field(default_factory=OrderedDict)
    _cache_bytes: int = 0
    # Multi-range prefetch (default off: planner is None). The planner returns up to N-1 more keyframe windows that
    # the extractor will want next; they are fetched together with the requested one in ONE request.
    planner: Callable[[int, int], list[tuple[int, int]]] | None = None
    cancelled: Callable[[], bool] = lambda: False
    multi_enabled: bool = True
    multi_requests: int = 0
    multi_parts: int = 0
    multi_bytes: int = 0
    multi_fallbacks: int = 0
    prefetch_waits: int = 0
    prefetch_hits: int = 0
    _multi_failures: int = 0
    _pending: dict = field(default_factory=dict)         # (start, end) -> Event set when that window is cached or abandoned

    def _lookup(self, start: int, end: int) -> bytes | None:
        with self._cache_lock:
            for (c_start, c_end), c_data in self._range_cache.items():
                if c_start <= start and end <= c_end:
                    self._range_cache.move_to_end((c_start, c_end))
                    return c_data[start - c_start:end - c_start + 1]
        return None

    def _cache_put(self, start: int, end: int, blob: bytes) -> None:
        with self._cache_lock:
            key = (start, end)
            if key in self._range_cache:
                self._cache_bytes -= len(self._range_cache.pop(key))
            self._range_cache[key] = blob
            self._cache_bytes += len(blob)
            while self._cache_bytes > 48 * 1024 * 1024 and self._range_cache:
                _, old = self._range_cache.popitem(last=False)
                self._cache_bytes -= len(old)

    def has_or_pending(self, start: int, end: int) -> bool:
        with self._cache_lock:
            if any(c0 <= start and end <= c1 for (c0, c1) in self._range_cache):
                return True
            return any(p0 <= start and end <= p1 for (p0, p1) in self._pending)

    def _covering_pending(self, start: int, end: int):
        with self._cache_lock:
            for (p0, p1), ev in self._pending.items():
                if p0 <= start and end <= p1:
                    return ev
        return None

    def _serve_multi(self, start: int, end: int, extra: list[tuple[int, int]], write: Callable[[bytes], None],
                     gate: Callable[[int], None] | None) -> bool:
        """Fetch (start, end) together with `extra` windows in one request. True = the requested bytes were written."""
        ranges = [(start, end)] + [r for r in dict.fromkeys(extra) if r != (start, end)]
        events = {r: threading.Event() for r in ranges}
        with self._cache_lock:
            for r in ranges[1:]:
                if r in self._pending:
                    events.pop(r)                               # someone else already carries it
            ranges = [r for r in ranges if r in events]
            self._pending.update({r: ev for r, ev in events.items() if r != (start, end)})
        written = [False]
        client_gone: list[OSError] = []

        def on_part(s: int, e: int, data: bytes) -> None:
            self._cache_put(s, e, data)                         # cache first, then wake the waiters
            with self._cache_lock:
                self._pending.pop((s, e), None)
            ev = events.get((s, e))
            if ev is not None:
                ev.set()
            self.multi_parts += 1
            self.multi_bytes += len(data)
            if (s, e) == (start, end) and not client_gone:
                try:
                    write(data)
                    written[0] = True
                except OSError as exc:                          # the reader left; keep filling the cache
                    client_gone.append(exc)

        try:
            self.multi_requests += 1
            self.upstream.read_multi(ranges, on_part, gate, self.cancelled)
            self._multi_failures = 0
        except MultiRangeUnsupported:
            self.multi_fallbacks += 1
            self.multi_enabled = False                          # the server cannot do it: stop asking
        except MultiRangeError:
            self.multi_fallbacks += 1
            self._multi_failures += 1
            if self._multi_failures >= 2:
                self.multi_enabled = False
        except UpstreamError as e:
            self.last_error = e
            if not written[0]:
                raise
        finally:
            with self._cache_lock:
                for r in ranges:
                    self._pending.pop(r, None)
            for ev in events.values():
                ev.set()                                        # waiters never hang: they fall back to a normal fetch
        if client_gone:
            raise client_gone[0]
        return written[0]

    def serve(self, start: int, end: int, write: Callable[[bytes], None]) -> None:
        for off, data in self.chunks:
            if off <= start and end < off + len(data):
                write(data[start - off:end - off + 1])
                return
        if self.shared_cache:
            hit = self._lookup(start, end)
            if hit is not None:
                self.prefetch_hits += 1
                write(hit)
                return
            ev = self._covering_pending(start, end)
            if ev is not None:                                  # a multi-range request is already bringing these bytes
                self.prefetch_waits += 1
                ev.wait(30.0)
                hit = self._lookup(start, end)
                if hit is not None:
                    self.prefetch_hits += 1
                    write(hit)
                    return
        gate = None
        if self.bucket is not None:
            bucket = self.bucket
            def gate(n):
                started = time.perf_counter()
                bucket.throttle(n, self.background)
                self.throttle_seconds += time.perf_counter() - started
        if (self.shared_cache and self.planner is not None and self.multi_enabled and self.background()
                and not self.cancelled()):
            extra = self.planner(start, end)
            if extra and self._serve_multi(start, end, extra, write, gate):
                return
        want = end - start + 1
        capture = bytearray() if (self.shared_cache and want <= 4 * 1024 * 1024) else None

        def sink(data: bytes) -> None:
            if capture is not None:
                capture.extend(data)
            write(data)

        try:
            self.upstream.read_range(start, end, sink, gate)
        except UpstreamError as e:
            self.last_error = e
            raise
        if capture is not None and len(capture) == want:
            self._cache_put(start, end, bytes(capture))


class RangeProxy:
    """Loopback-only HTTP range server in front of the vetted upstream fetchers (SR-10)."""

    def __init__(self):
        self._sources: dict[str, ProxySource] = {}
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_proxy_handler(self))
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda request, client_address: None
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="openseek-proxy", daemon=True)

    def start(self) -> "RangeProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread.is_alive():          # shutdown() would block forever if never started
            self.httpd.shutdown()
        self.httpd.server_close()

    def register(self, src: ProxySource) -> str:
        cap = secrets.token_hex(16)  # 128-bit, unguessable
        with self._lock:
            self._sources[cap] = src
        return cap

    def unregister(self, cap: str) -> None:
        with self._lock:
            self._sources.pop(cap, None)

    def get(self, cap: str) -> ProxySource | None:
        with self._lock:
            return self._sources.get(cap)

    def url(self, cap: str) -> str:
        return f"http://127.0.0.1:{self.port}/j/{cap}"


_PROXY_PATH = re.compile(r"^/j/([0-9a-f]{32})$")
_RANGE = re.compile(r"^bytes=(\d{1,15})-(\d{1,15})$")


def _make_proxy_handler(proxy: RangeProxy):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OpenSeekProxy"
        sys_version = ""

        def log_message(self, fmt, *args):  # never log paths (they carry caps)
            pass

        def _plain(self, code: int, err: str = "") -> None:
            body = b""
            self.send_response(code)
            if err:
                self.send_header("X-OpenSeek-Error", err)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self.close_connection = True
                return self._plain(403)
            m = _PROXY_PATH.match(self.path)
            src = proxy.get(m.group(1)) if m else None
            if src is None:
                return self._plain(404)
            r = _RANGE.match((self.headers.get("Range") or "").strip())
            if not r:
                return self._plain(416)
            start, end = int(r.group(1)), int(r.group(2))
            if end < start or start >= src.size or end - start + 1 > MAX_PROXY_RANGE:
                return self._plain(416)
            end = min(end, src.size - 1)
            src.proxied_requests += 1
            started = False

            def write(data: bytes) -> None:
                nonlocal started
                if not started:
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{src.size}")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    started = True
                self.wfile.write(data)

            try:
                src.serve(start, end, write)
            except UpstreamError as e:
                if not started:
                    return self._plain(502, e.code)
                self.close_connection = True   # mid-body: the reader sees a short body
            except (ConnectionError, OSError):
                self.close_connection = True

        def do_HEAD(self):  # noqa: N802
            self._plain(405)

        do_POST = do_PUT = do_DELETE = do_HEAD

    return H
