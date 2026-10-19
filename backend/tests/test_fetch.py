"""Vetted fetcher: preflight, SR-14 fingerprint, redirects (SR-9), budgets (SR-11), loopback proxy (SR-10)."""
from __future__ import annotations

import hashlib
import http.client
import socket

import pytest

from helpers import make_policy, remux
from openseek import mediasniff
from openseek.fetch import (PROBE_BYTES, Budget, ProxySource, RangeProxy, TokenBucket, Upstream, UpstreamError,
                            fingerprint)
from origin import Origin

A, B, PRIV = "a.example.com", "b.example.net", "intranet.example.org"


def up_for(origin, path, headers=None, budget=1 << 30, others=(), names=None):
    pol = make_policy([origin, *others], names or {A: ["127.0.0.1"], B: ["127.0.0.1"], PRIV: ["10.0.0.7"]})
    return Upstream(pol, f"http://{A}:{origin.port}{path}", headers or {}, Budget(budget))


def test_preflight_fingerprint_matches_sr14(origin, media):
    up = up_for(origin, "/f/main.mkv")
    pre = up.preflight()
    data = (media / "main.mkv").read_bytes()
    want = hashlib.sha256(b"v1" + len(data).to_bytes(8, "big") + hashlib.sha256(data[:65536]).digest()
                          + hashlib.sha256(data[-65536:]).digest()).hexdigest()
    assert pre.fp == want == fingerprint(len(data), data[:65536], data[-65536:])
    assert pre.size == len(data) and pre.fmt == "matroska" and pre.has_index is True
    assert [r["range"] for r in origin.log][:2] == [f"bytes=0-{PROBE_BYTES - 1}", f"bytes={len(data) - 65536}-{len(data) - 1}"]
    assert pre.head == data[:PROBE_BYTES]
    up.close()


def test_request_pacing_is_shared_across_upstreams_and_includes_preflight(origin):
    clock = [100.0]
    bucket = TokenBucket(2, burst=1, clock=lambda: clock[0],
                         sleep=lambda delay: clock.__setitem__(0, clock[0] + delay))
    a, b = up_for(origin, "/f/main.mkv"), up_for(origin, "/f/main.mkv")
    a.budget.request_bucket = b.budget.request_bucket = bucket
    try:
        a.preflight()
        b.preflight()
        requests = a.budget.requests + b.budget.requests
        assert requests >= 4
        assert clock[0] - 100 == pytest.approx((requests - 1) / 2)
    finally:
        a.close()
        b.close()


def test_fingerprint_ignores_url_and_hints(origin, media):
    """Same bytes behind different URLs -> same key; one changed byte in the tail -> different key."""
    import shutil
    shutil.copy(media / "main.mkv", media / "copy.mkv")
    a = up_for(origin, "/f/main.mkv").preflight().fp
    b = up_for(origin, "/f/copy.mkv").preflight().fp
    assert a == b
    with open(media / "copy.mkv", "r+b") as f:
        f.seek(-10, 2)
        x = f.read(1)
        f.seek(-10, 2)
        f.write(bytes([x[0] ^ 1]))
    assert up_for(origin, "/f/copy.mkv").preflight().fp != a


def test_mp4_and_index_detection(origin, media, tmp_path):
    assert up_for(origin, "/f/main.mp4").preflight().fmt == "mp4"
    origin.reset()
    pre = up_for(origin, "/f/nocues.mkv").preflight()
    assert pre.fmt == "matroska" and pre.has_index is False
    assert origin.bytes < 400_000                       # never scanned: head + tail + one tiny read
    remux(media / "main.mkv", media / "front.mkv", ["-cues_to_front", "1"])
    assert up_for(origin, "/f/front.mkv").preflight().has_index is True


def test_sniff_rejects_non_media():
    assert mediasniff.sniff(b"#EXTM3U\n#EXT-X-VERSION:3\n") is None
    assert mediasniff.sniff(b"<!doctype html><html>") is None
    assert mediasniff.sniff(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00") == "mp4"
    webm = bytes.fromhex("1a45dfa3") + bytes([0x80 | 7]) + bytes.fromhex("4282") + bytes([0x84]) + b"webm"
    assert mediasniff.sniff(webm) == "matroska"
    other = bytes.fromhex("1a45dfa3") + bytes([0x80 | 7]) + bytes.fromhex("4282") + bytes([0x84]) + b"evil"
    assert mediasniff.sniff(other) is None


def test_no_range_rejected_with_bounded_bytes(origin):
    origin.reset(range=False)
    with pytest.raises(UpstreamError) as ei:
        up_for(origin, "/f/main.mkv").preflight()
    assert ei.value.code == "range_unsupported"
    assert origin.bytes <= PROBE_BYTES, origin.bytes      # body never drained


@pytest.mark.parametrize("status,code", [(403, "source_expired"), (410, "source_expired"), (401, "source_expired"),
                                         (404, "source_unreachable"), (500, "source_unreachable")])
def test_status_classification(origin, status, code):
    origin.reset(raw={"/x": (status, "text/plain", b"no")})
    with pytest.raises(UpstreamError) as ei:
        up_for(origin, "/x").preflight()
    assert ei.value.code == code


def test_redirect_resolved_once_and_reused(origin):
    origin.reset(redirects={"/r": "/f/main.mkv?sig=abc"})
    up = up_for(origin, "/r")
    pre = up.preflight()
    up.read_bytes(1_000_000, 1_100_000)
    up.read_bytes(2_000_000, 2_100_000)
    paths = [r["path"] for r in origin.log]
    assert paths.count("/r") == 1 and all(p.startswith("/f/main.mkv") for p in paths[1:])
    assert pre.redirects == 1
    up.close()


def test_redirect_chain_limit(origin):
    hops = {f"/h{i}": f"/h{i + 1}" for i in range(6)}
    hops["/h6"] = "/f/main.mkv"
    origin.reset(redirects=hops)
    up_for(origin, "/h2").preflight()                     # 5 redirects (h2..h6): fine
    origin.reset(redirects=hops)
    with pytest.raises(UpstreamError) as ei:
        up_for(origin, "/h1").preflight()                 # 6 redirects: refused
    assert ei.value.reason == "too_many_redirects"
    assert origin.file_requests == 0


def test_redirect_to_loopback_blocked(origin, canary):
    origin.reset(redirects={"/r": f"http://127.0.0.1:{canary.port}/x"})
    with pytest.raises(UpstreamError) as ei:
        up_for(origin, "/r").preflight()
    assert ei.value.code == "source_forbidden"
    origin.reset(redirects={"/r": f"http://{PRIV}:{origin.port}/f/main.mkv"})
    with pytest.raises(UpstreamError) as ei:
        up_for(origin, "/r").preflight()
    assert ei.value.code == "source_forbidden"
    assert canary.hits == 0 and origin.file_requests == 0


def test_redirect_drops_headers_cross_origin(origin, media):
    other = Origin(media).start()
    try:
        fwd = {"User-Agent": "NuvioTest/1", "Referer": "https://addon.example/", "Origin": "https://addon.example"}
        origin.reset(redirects={"/same": "/cross", "/cross": f"http://{B}:{other.port}/f/main.mkv"})
        up = up_for(origin, "/same", fwd, others=[other])
        up.preflight()
        h1, h2 = origin.log[0]["headers"], origin.log[1]["headers"]
        assert h1["referer"] == h2["referer"] == "https://addon.example/"   # same origin: kept
        assert h1["user-agent"] == "NuvioTest/1"
        for r in other.log:
            h = r["headers"]
            assert "referer" not in h and "origin" not in h and h["user-agent"].startswith("OpenSeek/")
    finally:
        other.stop()


def test_header_allowlist_at_origin(origin, live_factory=None):
    from openseek.netpolicy import filter_forwarded_headers
    fwd = filter_forwarded_headers({"Cookie": "c=1", "Authorization": "Bearer t", "X-Foo": "1", "Referer": "r"})
    up_for(origin, "/f/main.mkv", fwd).preflight()
    for r in origin.log:
        h = r["headers"]
        assert "cookie" not in h and "authorization" not in h and "x-foo" not in h and h["referer"] == "r"


def test_budget_enforced_before_fetching(origin):
    with pytest.raises(UpstreamError) as ei:
        up_for(origin, "/f/main.mkv", budget=300_000).preflight()   # 256 KiB head + 64 KiB tail > budget
    assert ei.value.code == "budget_exceeded"
    assert origin.bytes <= 300_000


def test_expiry_mid_job(origin):
    up = up_for(origin, "/f/main.mkv")
    up.preflight()
    origin.reset(expired=True)
    with pytest.raises(UpstreamError) as ei:
        up.read_bytes(3_000_000, 3_100_000)
    assert ei.value.code == "source_expired"


def test_expired_link_reresolved_through_original_url(origin):
    """A signed final URL that answers 403 is re-resolved once from the original (addon) URL."""
    origin.reset(redirects={"/addon": "/f/main.mkv?sig=1"})
    up = up_for(origin, "/addon")
    up.preflight()
    origin.cfg.redirects["/addon"] = "/f/main.mp4?sig=2"      # new signed link for *different* bytes
    origin.cfg.raw["/f/main.mkv"] = (403, "text/plain", b"expired")
    with pytest.raises(UpstreamError) as ei:
        up.read_bytes(1_000_000, 1_000_100)
    assert ei.value.code == "source_expired" and ei.value.reason == "content_changed"


def test_dropped_connection_resumes(origin, media):
    up = up_for(origin, "/f/main.mkv")
    up.preflight()
    data = (media / "main.mkv").read_bytes()
    got = up.read_bytes(500_000, 1_500_000)
    assert got == data[500_000:1_500_001]


# ---------------------------------------------------------------- loopback proxy
@pytest.fixture()
def proxy(origin):
    p = RangeProxy().start()
    up = up_for(origin, "/f/main.mkv")
    pre = up.preflight()
    cap = p.register(ProxySource(up, pre.size, [(0, pre.head)]))
    yield p, cap, pre
    p.stop()
    up.close()


def _get(port, path, rng, host="127.0.0.1"):
    c = http.client.HTTPConnection(host, port, timeout=10)
    c.request("GET", path, headers={"Range": rng} if rng else {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def test_proxy_serves_ranges_and_cached_head(proxy, origin, media):
    p, cap, pre = proxy
    data = (media / "main.mkv").read_bytes()
    n0 = origin.hits
    st, body = _get(p.port, f"/j/{cap}", "bytes=0-1000")
    assert st == 206 and body == data[:1001] and origin.hits == n0        # served from preflight bytes
    st, body = _get(p.port, f"/j/{cap}", "bytes=3000000-3100000")
    assert st == 206 and body == data[3000000:3100001]


def test_proxy_unknown_cap_404(proxy):
    p, cap, _ = proxy
    assert _get(p.port, "/j/" + "0" * 32, "bytes=0-10")[0] == 404
    assert _get(p.port, f"/j/{cap}x", "bytes=0-10")[0] == 404
    assert _get(p.port, f"/j/{cap}?u=http://x", "bytes=0-10")[0] == 404
    assert _get(p.port, f"/j/{cap}", "bytes=0-")[0] == 416                 # only bounded ranges
    assert _get(p.port, f"/j/{cap}", "bytes=0-99999999")[0] == 416         # > 8 MiB
    assert _get(p.port, f"/j/{cap}", None)[0] == 416


def test_proxy_loopback_only(proxy):
    p, cap, _ = proxy
    assert p.httpd.server_address[0] == "127.0.0.1"
    from openseek.app import lan_addresses
    lan = [a for a in lan_addresses() if a != "127.0.0.1"]
    if not lan:
        pytest.skip("no non-loopback interface on this machine")
    with pytest.raises(OSError):
        socket.create_connection((lan[0], p.port), timeout=2).close()


def test_token_bucket_paces_only_background():
    t = [0.0]
    bucket = TokenBucket(1_000_000, burst=0, clock=lambda: t[0], sleep=lambda d: t.__setitem__(0, t[0] + d))
    for _ in range(10):
        bucket.throttle(200_000, lambda: True)
    assert t[0] == pytest.approx(2.0, rel=0.01)               # 2 MB at 1 MB/s
    t0 = t[0]
    bucket.throttle(5_000_000, lambda: False)                 # foreground: untouched
    assert t[0] == t0


def test_body_phase_timing_is_recorded_per_range_request(tmp_path):
    """The new download-timing counters: bytes, network read time, pacing and handoff time, per range request."""
    from helpers import make_policy
    from origin import Origin
    from openseek.fetch import Budget, Upstream
    data = bytes(range(256)) * 4096                                    # 1 MiB
    (tmp_path / "f.bin").write_bytes(data)
    o = Origin(tmp_path).start()
    try:
        up = Upstream(make_policy([o]), f"http://127.0.0.1:{o.port}/f/f.bin", {}, Budget(1 << 30))
        up.preflight()
        got = bytearray()
        up.read_range(0, 262_143, got.extend, None)
        b = up.stats.snapshot()["body"]
        assert bytes(got) == data[:262_144] and b["requests"] >= 1 and b["bytes"] >= 262_144
        assert up.stats.body_read_s > 0 and b["MBps_mean"] > 0 and b["read_ms_p50"] is not None and b["gate_s"] == 0
        up.close()
    finally:
        o.stop()
