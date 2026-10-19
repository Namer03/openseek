import http.client
import json
import socket
import time

from redact import redact_path
from server import parse_range

BLOB = bytes(range(256)) * 4096


def get(srv, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_parse_range():
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=95-200", 100) == (95, 99)
    assert parse_range("bytes=100-", 100) == "unsatisfiable"
    assert parse_range("bytes=0-1,5-6", 100) is None  # multi-range ignored -> 200
    assert parse_range(None, 100) is None


def test_full_and_partial_accounting(srv):
    srv.reset()
    r, body = get(srv, "/f/blob.bin")
    assert r.status == 200 and len(body) == 1 << 20
    r, body = get(srv, "/f/blob.bin", {"Range": "bytes=1000-1999"})
    assert r.status == 206 and body == BLOB[1000:2000]
    assert r.getheader("Content-Range") == f"bytes 1000-1999/{1 << 20}"
    st = srv.stats()
    assert st["requests"] == 2
    assert st["bytes"] == (1 << 20) + 1000
    assert [l["range"] for l in st["log"]] == [None, "bytes=1000-1999"]
    assert [l["status"] for l in st["log"]] == [200, 206]


def test_416(srv):
    srv.reset()
    assert get(srv, "/f/blob.bin", {"Range": f"bytes={1 << 20}-"})[0].status == 416


def test_client_abort_counts_partial_bytes(srv):
    srv.reset(rate_mbps=40)  # 5 MB/s -> a 1 MiB body takes ~0.2 s
    s = socket.create_connection(("127.0.0.1", srv.port))
    s.sendall(b"GET /f/blob.bin HTTP/1.1\r\nHost: x\r\n\r\n")
    got = 0
    while got < 100_000:
        got += len(s.recv(65536))
    s.close()  # abort mid-body
    rec = srv.stats()["log"][0]
    assert rec["aborted"] is True
    assert got <= rec["bytes"] < (1 << 20)
    assert rec["bytes"] - got < 600_000  # overcount bounded by socket buffers (documented)


def test_no_range_mode(srv):
    srv.reset(range=False)
    r, body = get(srv, "/f/blob.bin", {"Range": "bytes=0-9"})
    assert r.status == 200 and len(body) == 1 << 20
    assert r.getheader("Accept-Ranges") == "none"


def test_redirect_and_redaction(srv):
    srv.reset()
    r, _ = get(srv, "/r/blob.bin")
    assert r.status == 302
    loc = r.getheader("Location")
    assert loc.startswith("/f/blob.bin?sig=")
    assert get(srv, loc, {"Range": "bytes=0-0"})[0].status == 206
    paths = [l["path"] for l in srv.stats()["log"]]
    sig = loc.split("sig=")[1].split("&")[0]
    assert all(sig not in p for p in paths)
    assert "/f/blob.bin?sig=REDACTED&exp=REDACTED" in paths
    assert redact_path("/a?token=abc&x=1") == "/a?token=REDACTED&x=REDACTED"


def test_expire_after_requests(srv):
    srv.reset(expire_after_requests=2)
    codes = [get(srv, "/f/blob.bin", {"Range": "bytes=0-0"})[0].status for _ in range(4)]
    assert codes == [206, 206, 403, 403]


def test_expire_after_seconds(srv):
    srv.reset(expire_after_s=0.2)
    assert get(srv, "/f/blob.bin", {"Range": "bytes=0-0"})[0].status == 206
    time.sleep(0.3)
    assert get(srv, "/f/blob.bin", {"Range": "bytes=0-0"})[0].status == 403


def test_drop_mid_body(srv):
    srv.reset(drop_after_bytes=50_000, drops=1)
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.request("GET", "/f/blob.bin")
    r = c.getresponse()
    try:
        n = len(r.read())
    except http.client.IncompleteRead as e:
        n = len(e.partial)
    assert n == 50_000
    rec = srv.stats()["log"][0]
    assert rec["bytes"] == 50_000 and rec["aborted"]
    assert len(get(srv, "/f/blob.bin")[1]) == 1 << 20  # drops=1: next response intact


def test_throttle_rate(srv):
    srv.reset(rate_mbps=40)  # 5 MB/s
    t0 = time.perf_counter()
    get(srv, "/f/blob.bin")
    dt = time.perf_counter() - t0
    assert 0.17 < dt < 0.6, dt


def test_latency_and_new_conn_rtts(srv):
    srv.reset(latency_ms=50, new_conn_rtts=2)
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    t0 = time.perf_counter()
    c.request("GET", "/f/blob.bin", headers={"Range": "bytes=0-0"})
    c.getresponse().read()
    t1 = time.perf_counter()
    c.request("GET", "/f/blob.bin", headers={"Range": "bytes=0-0"})
    c.getresponse().read()
    t2 = time.perf_counter()
    c.close()
    assert t1 - t0 >= 0.15          # 1 RTT + 2 handshake RTTs on a new connection
    assert 0.05 <= t2 - t1 < 0.14   # keep-alive: 1 RTT


def test_path_traversal_rejected(srv):
    srv.reset()
    assert get(srv, "/f/..%2Fconftest.py")[0].status == 404
    assert get(srv, "/f/../x")[0].status == 404


def test_stats_endpoint_shows_inflight(srv):
    srv.reset(rate_mbps=8)  # 1 MB/s
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.request("GET", "/f/blob.bin")
    r = c.getresponse()
    r.read(100_000)
    _, body = get(srv, "/ctl/stats")
    st = json.loads(body)
    assert st["inflight"] == 1 and st["log"][0]["bytes"] >= 100_000
    c.close()
