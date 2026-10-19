"""SR-8 / SR-9 destination policy: the ABUSE-TESTS §3 matrix (and the PoC oracle's list) at the
unit level, plus vetted TLS connections (connect to the vetted IP, SNI + verification against the
hostname). API-level variants (400 source_forbidden, zero canary hits) are in test_security_api.py."""
from __future__ import annotations

import socket
import ssl

import pytest

from openseek.fetch import Budget, Upstream, UpstreamError
from openseek.netpolicy import Denied, DestinationPolicy, TestOnlyAllowance, address_allowed, filter_forwarded_headers
from origin import Origin
from tlsutil import TestCA

PUBLIC_V4 = "93.184.215.14"


def resolver_map(mapping):
    def r(host, port):
        if host in mapping:
            return mapping[host]
        raise socket.gaierror("nx")
    return r


PROD = DestinationPolicy(resolver=resolver_map({
    "evil.example.com": ["127.0.0.1"],                 # public-looking name -> loopback
    "split.example.com": [PUBLIC_V4, "10.1.2.3"],      # one bad answer poisons the set
    "meta.example.com": ["169.254.169.254"],
    "v6meta.example.com": ["fd00:ec2::254"],
    "mapped.example.com": ["::ffff:192.168.0.1"],
    "good.example.com": [PUBLIC_V4],
    "store-031.weur.tb-cdn.st": [PUBLIC_V4],
    "example.com": [PUBLIC_V4],
}))

ABUSE_MATRIX = [
    "http://127.0.0.1/", "http://127.1/", "http://[::1]/", "http://0x7f000001/", "http://2130706433/",
    "http://017700000001/", "http://localhost./", "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/latest/",
    "http://[fe80::1]/", "http://10.0.0.1/", "http://192.168.1.1/", "http://100.64.0.1/", "http://[fd00::1]/",
    "http://0.0.0.0/", "http://user:pass@example.com/", "file:///etc/passwd", "concat:a|b",
    "subfile:,,start,0,end,1,,:x", "data:video/mp4;base64,AA", "gopher://x/", "ftp://x/",
    "http://evil.example.com/", "https://good.example.com:22/",
]

POC_DENY = [
    "http://localhost/", "http://LOCALHOST/", "http://foo.localhost/", "http://nas.local/", "http://0177.0.0.1/",
    "http://[fe80::1%25eth0]/", "http://[fc00::1]/", "http://[::ffff:7f00:1]/", "http://[::127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[2002:c0a8:101::]/", "http://172.16.0.1/",
    "http://[::]/", "http://224.0.0.1/", "http://255.255.255.255/", "http://198.18.0.1/", "http://192.0.0.170/",
    "http://[ff02::1]/", "rtmp://example.com/x", "tcp://example.com:80", "udp://example.com:1234",
    "crypto:http://x", "https://example.com@127.0.0.1/", "https://example.com:0/", "https://example.com:99999/",
    "https://exa mple.com/", "https://example.com/\r\nX-Injected: 1", "https:///nohost", "//example.com/x",
    "https://[::1]:443/", "https://1.1.1.1.nip.io.:443/", "http://split.example.com/", "http://meta.example.com/",
    "http://v6meta.example.com/", "http://mapped.example.com/", "http://unknown-name.example.org/",
    "http://nas.home.arpa/", "http://router.lan/", "https://example.com:1234/",
]

ALLOW = [
    "https://store-031.weur.tb-cdn.st/dld/abc?token=x", "https://1.1.1.1/", "https://example.com:8443/x.mkv",
    "http://example.com/x.mkv", "https://[2606:4700::1111]/", "http://example.com:8080/a",
]


@pytest.mark.parametrize("url", ABUSE_MATRIX + POC_DENY)
def test_ssrf_matrix_denied(url):
    with pytest.raises(Denied):
        PROD.vet(url)


@pytest.mark.parametrize("url", ALLOW)
def test_public_destinations_allowed(url):
    t = PROD.vet(url)
    assert t.addrs and all(address_allowed(__import__("ipaddress").ip_address(a)) for a in t.addrs)


def test_http_can_be_disabled():
    p = DestinationPolicy(resolver=resolver_map({"example.com": [PUBLIC_V4]}), allow_http=False)
    with pytest.raises(Denied):
        p.vet("http://example.com/x.mkv")
    assert p.vet("https://example.com/x.mkv").scheme == "https"


def test_allowed_ports_configurable():
    p = DestinationPolicy(resolver=resolver_map({"example.com": [PUBLIC_V4]}), allowed_ports=[443])
    with pytest.raises(Denied):
        p.vet("http://example.com/x")
    p.vet("https://example.com/x")


def test_test_allowance_is_exact_ip_and_port():
    a = TestOnlyAllowance(frozenset({("127.0.0.1", 5555)}))
    p = DestinationPolicy(resolver=resolver_map({"m.example.com": ["127.0.0.1"]}), allowance=a)
    assert p.vet("http://m.example.com:5555/x").addrs == ("127.0.0.1",)
    for bad in ("http://m.example.com:5556/x", "http://127.0.0.2:5555/", "http://[::1]:5555/"):
        with pytest.raises(Denied):
            p.vet(bad)


def test_no_env_switch_relaxes_policy(monkeypatch):
    """The allowance cannot come from configuration: Settings has no such field and the Core
    builds a policy without one."""
    from openseek.config import Settings
    for k in ("OPENSEEK_ALLOW_LOOPBACK", "OPENSEEK_TEST_ALLOW", "OPENSEEK_SSRF_ALLOW"):
        monkeypatch.setenv(k, "127.0.0.1:1")
    s = Settings.from_env()
    assert not any("allow" in f and f not in ("allowed_ports", "allow_http_sources", "allow_public_clients")
                   for f in s.__dataclass_fields__)
    p = DestinationPolicy(s.allowed_ports, s.allow_http_sources)
    assert p.allowance is None
    with pytest.raises(Denied):
        p.vet("http://127.0.0.1:1/")


def test_refuses_unverified_tls_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        DestinationPolicy(ssl_context=ctx)


def test_header_allowlist_filter():
    got = filter_forwarded_headers({"User-Agent": "UA", "referer": "https://r/", "Origin": "https://o",
                                    "Cookie": "c=1", "Authorization": "Bearer x", "X-Foo": "1",
                                    "Host": "evil", "Range": "bytes=0-", "User-Agent ": "dup",
                                    "Origin\r\nX": "inj"})
    assert got == {"User-Agent": "dup", "Referer": "https://r/", "Origin": "https://o"} or \
        got == {"User-Agent": "UA", "Referer": "https://r/", "Origin": "https://o"}
    assert filter_forwarded_headers({"Referer": "a\r\nInjected: 1"}) == {}
    assert filter_forwarded_headers({"Referer": "x" * 2000}) == {}


# ---------------------------------------------------------------- vetted TLS
@pytest.fixture()
def tls_origin(media):
    ca = TestCA()
    o = Origin(media, tls_context=ca.server_context("cdn.example.com")).start()
    yield ca, o
    o.stop()


def test_tls_connects_to_vetted_ip_with_sni_and_verification(tls_origin):
    ca, o = tls_origin
    allow = TestOnlyAllowance(frozenset({("127.0.0.1", o.port)}))
    p = DestinationPolicy(resolver=resolver_map({"cdn.example.com": ["127.0.0.1"]}), allowance=allow,
                          ssl_context=ca.client_context())
    up = Upstream(p, f"https://cdn.example.com:{o.port}/f/main.mkv", {}, Budget(1 << 30))
    pre = up.preflight()
    assert pre.fmt == "matroska"
    assert o.log[0]["headers"]["host"] == f"cdn.example.com:{o.port}"   # Host = hostname, not the IP
    assert o.log[0]["headers"]["accept-encoding"] == "identity"
    up.close()


def test_tls_hostname_mismatch_refused(tls_origin):
    ca, o = tls_origin
    allow = TestOnlyAllowance(frozenset({("127.0.0.1", o.port)}))
    p = DestinationPolicy(resolver=resolver_map({"other.example.com": ["127.0.0.1"]}), allowance=allow,
                          ssl_context=ca.client_context())
    up = Upstream(p, f"https://other.example.com:{o.port}/f/main.mkv", {}, Budget(1 << 30))
    with pytest.raises(UpstreamError) as ei:
        up.preflight()
    assert ei.value.code == "source_unreachable" and ei.value.reason == "tls"
    assert o.hits == 0


def test_tls_untrusted_ca_refused(tls_origin):
    _, o = tls_origin
    allow = TestOnlyAllowance(frozenset({("127.0.0.1", o.port)}))
    p = DestinationPolicy(resolver=resolver_map({"cdn.example.com": ["127.0.0.1"]}), allowance=allow)
    up = Upstream(p, f"https://cdn.example.com:{o.port}/f/main.mkv", {}, Budget(1 << 30))
    with pytest.raises(UpstreamError):
        up.preflight()
    assert o.hits == 0


def test_https_to_http_downgrade_refused(tls_origin, origin):
    ca, o = tls_origin
    allow = TestOnlyAllowance(frozenset({("127.0.0.1", o.port), ("127.0.0.1", origin.port)}))
    p = DestinationPolicy(resolver=resolver_map({"cdn.example.com": ["127.0.0.1"], "plain.example.com": ["127.0.0.1"]}),
                          allowance=allow, ssl_context=ca.client_context())
    o.reset(redirects={"/go": f"http://plain.example.com:{origin.port}/f/main.mkv"})
    up = Upstream(p, f"https://cdn.example.com:{o.port}/go", {}, Budget(1 << 30))
    with pytest.raises(UpstreamError) as ei:
        up.preflight()
    assert ei.value.code == "source_forbidden" and ei.value.reason == "downgrade"
    assert origin.hits == 0
