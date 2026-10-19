"""Test origin: a threaded HTTP(S) range server with byte accounting, request/header logging,
redirect chains, expiry and no-Range modes. Plays the role of the remote CDN / addon in tests.
Only ever bound to 127.0.0.1."""
from __future__ import annotations

import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHUNK = 16 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass
class OriginConfig:
    range: bool = True
    expire_after_requests: int = 0          # /f/ requests allowed before 403 (0 = never)
    expired: bool = False                   # every /f/ request answers 403
    rate_bps: float = 0.0                   # pacing per response (0 = unlimited)
    latency_s: float = 0.0
    multirange: str = "off"                 # off | ok | single | drop | bad | garbage | truncate | whole
    multi_extra_latency_s: float = 0.0      # added first-byte delay for multipart answers (a real CDN was ~0.25 s slower)
    redirects: dict = field(default_factory=dict)  # path -> absolute or relative Location
    raw: dict = field(default_factory=dict)        # path -> (status, content_type, body)


class Origin:
    def __init__(self, root: Path | None = None, tls_context: ssl.SSLContext | None = None):
        self.root = Path(root) if root else None
        self.cfg = OriginConfig()
        self.lock = threading.Lock()
        self.log: list[dict] = []
        self.bytes = 0
        self.file_requests = 0
        self.multi_requests = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda request, client_address: None
        if tls_context is not None:
            self.httpd.socket = tls_context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.scheme = "https" if tls_context else "http"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def start(self) -> "Origin":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset(self, **cfg) -> None:
        with self.lock:
            self.cfg = OriginConfig(**cfg)
            self.log = []
            self.bytes = 0
            self.file_requests = 0
            self.multi_requests = 0

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"{self.scheme}://{host}:{self.port}{path}"

    @property
    def hits(self) -> int:
        return len(self.log)

    def _handler(self):
        origin = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                try:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

            def _rec(self, status: int) -> dict:
                rec = {"path": self.path, "status": status, "range": self.headers.get("Range"),
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "bytes": 0, "t": time.time()}
                with origin.lock:
                    origin.log.append(rec)
                return rec

            def _send(self, status: int, body: bytes, ctype: str = "text/plain", extra: dict | None = None):
                rec = self._rec(status)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                    rec["bytes"] = len(body)
                    with origin.lock:
                        origin.bytes += len(body)

            def do_GET(self):  # noqa: N802
                cfg = origin.cfg
                if cfg.latency_s:
                    time.sleep(cfg.latency_s)
                path = self.path.split("?", 1)[0]
                if path in cfg.redirects:
                    return self._send(302, b"", extra={"Location": cfg.redirects[path]})
                if path in cfg.raw:
                    st, ct, body, *hdrs = cfg.raw[path]          # optional 4th item: response headers
                    return self._send(st, body, ct, extra=hdrs[0] if hdrs else None)
                if not path.startswith("/f/") or origin.root is None:
                    return self._send(404, b"404\n")
                with origin.lock:
                    origin.file_requests += 1
                    n = origin.file_requests
                if cfg.expired or (cfg.expire_after_requests and n > cfg.expire_after_requests):
                    return self._send(403, b"403\n")
                name = path[3:]
                fp = origin.root / name
                if "/" in name or "\\" in name or not fp.is_file():
                    return self._send(404, b"404\n")
                size = fp.stat().st_size
                rh = (self.headers.get("Range") or "").strip()
                if cfg.multirange != "off" and "," in rh and rh.startswith("bytes="):
                    return self._multi(fp, size, rh, cfg.multirange)
                start, end, status = 0, size - 1, 200
                m = _RANGE.match(rh)
                if cfg.range and m and (m.group(1) or m.group(2)):
                    a, b = m.groups()
                    if a == "":
                        start = max(0, size - int(b))
                    else:
                        start = int(a)
                        end = min(int(b), size - 1) if b else size - 1
                    if start >= size or end < start:
                        return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                    status = 206
                rec = self._rec(status)
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if not cfg.range:
                    self.send_header("Accept-Ranges", "none")
                self.end_headers()
                remaining = end - start + 1
                try:
                    with open(fp, "rb") as f:
                        f.seek(start)
                        while remaining > 0:
                            buf = f.read(min(CHUNK, remaining))
                            if not buf:
                                break
                            if cfg.rate_bps:
                                time.sleep(len(buf) / cfg.rate_bps)
                            self.wfile.write(buf)
                            remaining -= len(buf)
                            rec["bytes"] += len(buf)
                            with origin.lock:
                                origin.bytes += len(buf)
                except (ConnectionError, OSError):
                    self.close_connection = True

            def _multi(self, fp, size, rh, mode):
                rs = []
                for part in rh[6:].split(","):
                    a, b = part.strip().split("-")
                    rs.append((int(a), min(int(b), size - 1)))
                rec = self._rec(206)
                with origin.lock:
                    origin.multi_requests += 1
                if origin.cfg.multi_extra_latency_s:
                    time.sleep(origin.cfg.multi_extra_latency_s)
                if mode == "whole":                       # a CDN that ignores the extra ranges
                    rec["status"] = 200
                    self.send_response(200)
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    return
                if mode == "single":
                    a, b = rs[0]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
                    self.send_header("Content-Length", str(b - a + 1))
                    self.end_headers()
                    with open(fp, "rb") as f:
                        f.seek(a)
                        self.wfile.write(f.read(b - a + 1))
                    return
                body = bytearray()
                with open(fp, "rb") as f:
                    for i, (a, b) in enumerate(rs):
                        if mode == "drop" and i == 2:
                            continue
                        f.seek(a)
                        shown = (a, b - 1) if mode == "bad" and i == 2 else (a, b)
                        body += (f"--MRB\r\nContent-Type: application/octet-stream\r\n"
                                 f"Content-Range: bytes {shown[0]}-{shown[1]}/{size}\r\n\r\n").encode()
                        body += f.read(b - a + 1) + b"\r\n"
                body += b"--MRB--\r\n"
                payload = bytes(body)
                if mode == "garbage":
                    payload = b"this is not multipart" * 50
                self.send_response(206)
                self.send_header("Content-Type", "multipart/byteranges; boundary=MRB")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    if mode == "truncate":
                        self.wfile.write(payload[:len(payload) // 2])
                        self.close_connection = True
                        self.wfile.flush()
                        self.connection.close()
                        return
                    step = 64 * 1024
                    for i in range(0, len(payload), step):
                        if cfg_rate := origin.cfg.rate_bps:
                            time.sleep(min(step, len(payload) - i) / cfg_rate)
                        self.wfile.write(payload[i:i + step])
                    rec["bytes"] = len(payload)
                    with origin.lock:
                        origin.bytes += len(payload)
                except (ConnectionError, OSError):
                    self.close_connection = True

            do_HEAD = do_GET

        return H


class Canary:
    """Records every connection attempt (it must stay at zero in SSRF tests)."""

    def __init__(self):
        self.hits = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            self.hits += 1
            c.close()

    def stop(self):
        self._stop = True
        self.sock.close()
