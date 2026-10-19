"""Controlled HTTP test server for the OpenSeek Gate 1 benchmark (stdlib only, threaded).

Routes
  GET|HEAD /f/<name>[?sig=..]   serve fixtures/<name> (Range support configurable)
  GET|HEAD /r/<name>            302 -> /f/<name>?sig=<random>&exp=<ts>   (CDN signed-redirect hop)
  GET|HEAD /u/<any>             accounting proxy to an upstream URL set in-process (real internet / TorBox)
  GET  /ctl/stats               JSON accounting since last reset (also written to --stats-file)
  POST /ctl/reset               clear accounting; optional JSON body = full config to apply
  POST /ctl/config              merge JSON body into config (does not clear accounting)

Config keys (see Config): range, latency_ms, new_conn_rtts, rate_mbps, conn_rate_mbps,
  expire_after_requests, expire_after_s, drop_after_bytes, drops

Accounting: every request records method, redacted path, Range header, status, body bytes
actually written to the socket (counted per chunk, so partially-sent bodies when the client
aborts are included), whether it was aborted, duration, connection id and User-Agent.
Note: "written to the socket" can exceed "received by the client" by at most the socket send
buffer (SO_SNDBUF is pinned to 128 KiB) plus the receiver's window; this is the same quantity a
CDN would bill as egress.

Throttling models a remote CDN: a shared (aggregate) token bucket = the backend's downlink,
optional per-connection cap, a per-request latency (1 RTT before the response headers) and
`new_conn_rtts` extra RTTs on the first request of a TCP connection (TCP + TLS handshakes).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from redact import redact_path

CHUNK = 16 * 1024
SNDBUF = 128 * 1024


@dataclass
class Config:
    range: bool = True
    latency_ms: float = 0.0
    new_conn_rtts: int = 0
    rate_mbps: float = 0.0          # aggregate cap across all connections (0 = unlimited)
    conn_rate_mbps: float = 0.0     # per-response cap (0 = unlimited)
    expire_after_requests: int = 0  # /f/ requests allowed before 403 (0 = never)
    expire_after_s: float = 0.0     # seconds after reset before 403 (0 = never)
    drop_after_bytes: int = 0       # abort the connection after N body bytes (0 = never)
    drops: int = 1                  # how many responses to drop (-1 = every response)

    def update(self, d: dict) -> None:
        names = {f.name for f in fields(self)}
        for k, v in d.items():
            if k not in names:
                raise KeyError(k)
            setattr(self, k, type(getattr(self, k))(v))


class Pacer:
    """FIFO virtual-time pacer: each chunk reserves len/rate seconds of link time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next_free = 0.0

    def wait(self, nbytes: int, rate_bps: float) -> None:
        if rate_bps <= 0:
            return
        with self.lock:
            now = time.perf_counter()
            start = max(now, self.next_free)
            self.next_free = start + nbytes / rate_bps
            delay = self.next_free - now
        if delay > 0:
            time.sleep(delay)


class State:
    def __init__(self, root: Path, stats_file: Path | None):
        self.root = root
        self.stats_file = stats_file
        self.lock = threading.Lock()
        self.cfg = Config()
        self.upstream_url: str | None = None  # accounting-proxy target (never logged)
        self.pacer = Pacer()
        self.conn_seq = 0
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.t0 = time.time()
            self.requests: list[dict] = []
            self.file_requests = 0
            self.drops_done = 0
            self.connections = 0
            self.active = 0

    def snapshot(self) -> dict:
        with self.lock:
            reqs = [dict(r) for r in self.requests]
            by_ua: dict[str, dict] = {}
            for r in reqs:
                u = by_ua.setdefault(r["ua"], {"requests": 0, "bytes": 0})
                u["requests"] += 1
                u["bytes"] += r["bytes"]
            snap = {
                "config": asdict(self.cfg),
                "since": self.t0,
                "connections": self.connections,
                "requests": len(reqs),
                "file_requests": sum(1 for r in reqs if r["path"].startswith(("/f/", "/u/"))),
                "inflight": sum(1 for r in reqs if r.get("inflight")),
                "bytes": sum(r["bytes"] for r in reqs),
                "by_ua": by_ua,
                "log": reqs,
            }
        if self.stats_file:
            self.stats_file.write_text(json.dumps(snap, indent=1))
        return snap


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(h: str | None, size: int):
    """Return (start, end_inclusive) or None (no/ignored range) or 'unsatisfiable'."""
    if not h:
        return None
    m = _RANGE_RE.match(h.strip())
    if not m:
        return None  # multi-range / malformed: RFC 9110 allows ignoring -> 200 full
    a, b = m.groups()
    if a == "" and b == "":
        return None
    if a == "":
        n = int(b)
        if n == 0:
            return "unsatisfiable"
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    if start >= size or end < start:
        return "unsatisfiable"
    return start, min(end, size - 1)


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OpenSeekBench/1"

        def setup(self):
            super().setup()
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            with state.lock:
                state.conn_seq += 1
                state.connections += 1
                self.conn_id = state.conn_seq
            self.req_on_conn = 0

        def log_message(self, fmt, *args):  # silence default logging (it would print raw paths)
            pass

        # ---------------------------------------------------------------- helpers
        def _rec(self, status: int, nbytes: int, aborted: bool, t0: float, rng=None, inflight=False):
            rec = {
                "t": round(t0 - state.t0, 4),
                "dur": round(time.time() - t0, 4),
                "method": self.command,
                "path": redact_path(self.path),
                "range": rng,
                "status": status,
                "bytes": nbytes,
                "aborted": aborted,
                "inflight": inflight,
                "conn": self.conn_id,
                "ua": (self.headers.get("User-Agent") or "")[:60],
            }
            with state.lock:
                state.requests.append(rec)
            return rec

        def _finish(self, rec: dict, t0: float, aborted: bool):
            with state.lock:
                rec["dur"] = round(time.time() - t0, 4)
                rec["aborted"] = aborted
                rec["inflight"] = False

        def _send_body(self, rec: dict, t0: float, read_chunk):
            """Write body chunks from read_chunk(n) with throttling / drop simulation, live accounting."""
            cfg = state.cfg
            aborted = False
            drop_at = 0
            if cfg.drop_after_bytes:
                with state.lock:
                    if cfg.drops < 0 or state.drops_done < cfg.drops:
                        state.drops_done += 1
                        drop_at = cfg.drop_after_bytes
            conn_pacer = Pacer()
            sent = 0
            try:
                while True:
                    n = CHUNK
                    if drop_at:
                        n = min(n, drop_at - sent)
                    buf = read_chunk(n)
                    if not buf:
                        break
                    state.pacer.wait(len(buf), cfg.rate_mbps * 125000)
                    conn_pacer.wait(len(buf), cfg.conn_rate_mbps * 125000)
                    self.wfile.write(buf)
                    sent += len(buf)
                    rec["bytes"] = sent
                    if drop_at and sent >= drop_at:
                        aborted = True
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        break
            except (ConnectionError, OSError):
                aborted = True
                self.close_connection = True
            self._finish(rec, t0, aborted)

        def _latency(self):
            cfg = state.cfg
            rtts = 1 + (cfg.new_conn_rtts if self.req_on_conn == 0 else 0)
            self.req_on_conn += 1
            if cfg.latency_ms > 0:
                time.sleep(cfg.latency_ms * rtts / 1000.0)

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _simple(self, code: int, t0: float, rng=None):
            body = f"{code}\n".encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            n = 0
            if self.command != "HEAD":
                self.wfile.write(body)
                n = len(body)
            self._rec(code, n, False, t0, rng)

        # ---------------------------------------------------------------- control
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if self.path == "/ctl/reset":
                state.cfg = Config()
                state.cfg.update(body)
                state.reset()
                return self._json(200, asdict(state.cfg))
            if self.path == "/ctl/config":
                try:
                    state.cfg.update(body)
                except KeyError as e:
                    return self._json(400, {"error": f"unknown key {e}"})
                return self._json(200, asdict(state.cfg))
            return self._json(404, {"error": "no such control"})

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            t0 = time.time()
            path = self.path.split("?", 1)[0]
            if path == "/ctl/stats":
                return self._json(200, state.snapshot())
            if path.startswith("/r/"):
                self._latency()
                name = path[3:]
                loc = f"/f/{name}?sig={secrets.token_hex(16)}&exp={int(time.time()) + 3600}"
                self.send_response(302)
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return self._rec(302, 0, False, t0)
            if path.startswith("/u/"):
                with state.lock:
                    state.active += 1
                try:
                    return self._proxy(t0)
                finally:
                    with state.lock:
                        state.active -= 1
            if not path.startswith("/f/"):
                return self._simple(404, t0)
            self._serve_file(path[3:], t0)

        def _serve_file(self, name: str, t0: float):
            with state.lock:
                state.active += 1
            try:
                self._serve_file2(name, t0)
            finally:
                with state.lock:
                    state.active -= 1

        def _serve_file2(self, name: str, t0: float):
            cfg = state.cfg
            rng_hdr = self.headers.get("Range")
            self._latency()
            with state.lock:
                state.file_requests += 1
                nreq = state.file_requests
                expired = (cfg.expire_after_requests and nreq > cfg.expire_after_requests) or (
                    cfg.expire_after_s and time.time() - state.t0 > cfg.expire_after_s)
            if expired:
                return self._simple(403, t0, rng_hdr)
            if "/" in name or "\\" in name or name.startswith("."):
                return self._simple(404, t0, rng_hdr)
            fp = state.root / name
            if not fp.is_file():
                return self._simple(404, t0, rng_hdr)
            size = fp.stat().st_size
            start, end, status = 0, size - 1, 200
            if cfg.range:
                r = parse_range(rng_hdr, size)
                if r == "unsatisfiable":
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return self._rec(416, 0, False, t0, rng_hdr)
                if r is not None:
                    start, end = r
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "video/x-matroska" if name.endswith(".mkv") else "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes" if cfg.range else "none")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return self._rec(status, 0, False, t0, rng_hdr)
            rec = self._rec(status, 0, False, t0, rng_hdr, inflight=True)
            with open(fp, "rb") as f:
                f.seek(start)
                remaining = [length]

                def rd(n):
                    n = min(n, remaining[0])
                    if n <= 0:
                        return b""
                    b = f.read(n)
                    remaining[0] -= len(b)
                    return b

                self._send_body(rec, t0, rd)

        # ---------------------------------------------------------------- accounting proxy
        def _proxy(self, t0: float):
            """GET /u/<anything> -> state.upstream_url (single target, kept in memory, never logged).
            Forwards Range, follows redirects, streams the body back with byte accounting."""
            import urllib.error
            import urllib.request

            rng_hdr = self.headers.get("Range")
            if not state.upstream_url:
                return self._simple(404, t0, rng_hdr)
            hdrs = {"User-Agent": self.headers.get("User-Agent") or "OpenSeek-bench"}
            if rng_hdr:
                hdrs["Range"] = rng_hdr
            req = urllib.request.Request(state.upstream_url, headers=hdrs, method=self.command)
            try:
                up = urllib.request.urlopen(req, timeout=20)
            except urllib.error.HTTPError as e:
                return self._simple(e.code, t0, rng_hdr)
            except (urllib.error.URLError, OSError):
                return self._simple(502, t0, rng_hdr)
            with up:
                self.send_response(up.status)
                for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if up.headers.get(h):
                        self.send_header(h, up.headers[h])
                self.end_headers()
                rec = self._rec(up.status, 0, False, t0, rng_hdr, inflight=True)
                if self.command == "HEAD":
                    return self._finish(rec, t0, False)
                self._send_body(rec, t0, lambda n: up.read(n))

    return H


class BenchServer:
    """Run the server in-process (used by run_bench.py and tests)."""

    def __init__(self, root: Path, host: str = "127.0.0.1", port: int = 0, stats_file: Path | None = None):
        self.state = State(Path(root), stats_file)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.state))
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda request, client_address: None  # client resets are expected
        self.port = self.httpd.server_address[1]
        self.base = f"http://{host}:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset(self, **cfg):
        self.state.cfg = Config()
        self.state.cfg.update(cfg)
        self.state.reset()

    def stats(self, wait: float = 3.0) -> dict:
        # allow in-flight file responses to notice the client went away and record their bytes
        deadline = time.time() + wait
        time.sleep(0.02)
        while self.state.active > 0 and time.time() < deadline:
            time.sleep(0.01)
        return self.state.snapshot()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent / "fixtures"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--stats-file")
    ap.add_argument("--config", default="{}", help="initial config JSON")
    a = ap.parse_args()
    s = BenchServer(Path(a.root), a.host, a.port, Path(a.stats_file) if a.stats_file else None)
    s.state.cfg.update(json.loads(a.config))
    s.httpd.handle_error = lambda request, client_address: None
    print(f"serving {a.root} on {s.base}", flush=True)
    try:
        s.httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
