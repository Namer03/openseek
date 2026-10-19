"""multirange_compare end to end on a real MKV served by a local multi-range origin."""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import multirange_compare as mc  # noqa: E402
from helpers import make_policy  # noqa: E402

from helpers import FIXTURES as ALT, FIXTURES_HINT
FILE = ALT / "ok_10min.mkv"
pytestmark = pytest.mark.skipif(not FILE.exists(), reason=FIXTURES_HINT)


def _server(multi: bool = True):
    data = FILE.read_bytes()
    log = {"requests": 0}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            log["requests"] += 1
            rng = self.headers.get("Range", "")
            rs = [tuple(int(x) for x in r.split("-")) for r in rng.removeprefix("bytes=").split(",")]
            rs = [(a, min(b, len(data) - 1)) for a, b in rs]
            if len(rs) == 1 or not multi:
                a, b = rs[0]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                self.send_header("Content-Length", str(b - a + 1))
                self.end_headers()
                self.wfile.write(data[a:b + 1])
                return
            body = b""
            for a, b in rs:
                body += (f"--BND\r\nContent-Type: video/x-matroska\r\nContent-Range: bytes {a}-{b}/{len(data)}\r\n\r\n").encode()
                body += data[a:b + 1] + b"\r\n"
            body += b"--BND--\r\n"
            self.send_response(206)
            self.send_header("Content-Type", "multipart/byteranges; boundary=BND")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class O:
        port = httpd.server_address[1]
    return httpd, O, len(data), log


def test_windows_come_from_the_index_of_this_exact_file_and_both_arms_return_identical_bytes():
    httpd, o, size, log = _server()
    try:
        link = f"http://127.0.0.1:{o.port}/f.mkv"
        rep = mc.run(link, make_policy([o]), size, rps=1000, first_slot=5, n=8)
        assert "stopped" not in rep, rep
        assert rep["bytes_identical_per_window"] == [True] * 8
        assert rep["multi"]["parts"] == 8 and rep["multi"]["parts_ok"] and rep["multi"]["requests"] == 1
        assert rep["single"]["requests"] == 8 and all(r["ok"] for r in rep["single"]["per_request"])
        assert all(rep["window_contains_cluster_id"]), rep["window_contains_cluster_id"]          # offsets land on this file's clusters
        assert rep["multi"]["bytes"] >= rep["single"]["bytes"]                                   # multipart framing only adds bytes
        assert rep["total_bytes_both_arms"] <= 2 * 8 * mc.WINDOW + 8 * 1024
    finally:
        httpd.shutdown()


def test_a_different_file_size_is_refused_before_any_window_is_requested():
    httpd, o, size, log = _server()
    try:
        link = f"http://127.0.0.1:{o.port}/f.mkv"
        with pytest.raises(mc.Unexpected):
            mc.run(link, make_policy([o]), size + 1, rps=1000, first_slot=5, n=8)
    finally:
        httpd.shutdown()


def test_single_range_only_server_stops_the_multi_arm():
    httpd, o, size, log = _server(multi=False)
    try:
        link = f"http://127.0.0.1:{o.port}/f.mkv"
        rep = mc.run(link, make_policy([o]), size, rps=1000, first_slot=5, n=8)
        assert "stopped" in rep and "multi" not in rep
    finally:
        httpd.shutdown()
