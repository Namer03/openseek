"""The probe's verdict logic against local servers that behave like each kind of CDN."""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import multirange_probe as mp  # noqa: E402
from helpers import make_policy  # noqa: E402

TOTAL = 80 * (1 << 30)


def _server(mode: str):
    served = {"bytes": 0, "requests": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            served["requests"] += 1
            rng = self.headers.get("Range", "")
            parts = [tuple(int(x) for x in r.split("-")) for r in rng.removeprefix("bytes=").split(",") if "-" in r]
            if mode == "ignore":                                   # 200 and an endless body
                self.send_response(200)
                self.send_header("Content-Length", str(TOTAL))
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(b"x" * 8192)
                        served["bytes"] += 8192
                except OSError:
                    return
            if mode == "single":
                a, b = parts[0]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{TOTAL}")
                self.send_header("Content-Length", str(b - a + 1))
                self.end_headers()
                self.wfile.write(b"y" * (b - a + 1))
                return
            boundary = "BOUNDARY123"
            body = b""
            for a, b in parts:
                shown = (a, b + 1) if mode == "bad_range" and (a, b) == parts[2] else (a, b)
                n = b - a + 1 if mode != "short_part" or (a, b) != parts[3] else 10
                body += (f"--{boundary}\r\nContent-Type: application/octet-stream\r\n"
                         f"Content-Range: bytes {shown[0]}-{shown[1]}/{TOTAL}\r\n\r\n").encode() + b"z" * n + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            self.send_response(206)
            self.send_header("Content-Type", f"multipart/byteranges; boundary={boundary}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class O:
        port = httpd.server_address[1]
    return httpd, O, served


@pytest.mark.parametrize("mode,expected", [("ok", "SUCCESS"), ("single", "SINGLE_RANGE_ONLY"), ("ignore", "IGNORED_RANGE"),
                                           ("bad_range", "UNEXPECTED"), ("short_part", "UNEXPECTED")])
def test_probe_verdicts(mode, expected):
    httpd, o, served = _server(mode)
    try:
        rep = mp.probe(f"http://127.0.0.1:{o.port}/f.mkv", make_policy([o]), mp.default_ranges())
        assert rep["verdict"] == expected, rep
        assert served["requests"] == 1 and rep["requests_sent"] == 1                        # one request, never retried
        if mode == "ignore":
            assert rep["truncated_at_cap"] and rep["body_bytes_read"] <= mp.CAP            # never downloads the movie
            assert served["bytes"] <= mp.CAP + 1_000_000                                    # socket buffers, not the file
    finally:
        httpd.shutdown()


def test_default_ranges_are_small_disjoint_and_32k():
    r = mp.default_ranges()
    assert len(r) == 8 and sum(b - a + 1 for a, b in r) == 32 * 1024
    assert all(r[i][1] < r[i + 1][0] for i in range(7))
