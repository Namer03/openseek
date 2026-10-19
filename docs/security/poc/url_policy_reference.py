#!/usr/bin/env python3
"""OpenSeek URL / destination policy - REFERENCE ORACLE, not production code.

Implements SR-URL-* from SECURITY-REQUIREMENTS.md so the QA agent has an
executable oracle for the SSRF matrix in ABUSE-TESTS.md. Production code must
apply the same rules inside the vetted outbound fetcher (resolve -> check every
address -> connect to the checked address), on the first hop AND on every
redirect hop.

Self-test:  python -I url_policy_reference.py
Only literal addresses and 'localhost' are resolved; no outbound traffic.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"https", "http"}          # http only if settings.allow_http_sources
MAX_URL_LEN = 4096
# LDH labels, at least one dot, TLD alphabetic (rejects "2130706433", "0x7f000001", "127.1").
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DNS_NAME = re.compile(rf"^(?:{_LABEL}\.)+[a-z][a-z0-9-]{{0,62}}$")
_DOTTED_QUAD = re.compile(r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$")
DENY_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
DENY_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".intranet", ".corp")


class Denied(ValueError):
    pass


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in ipaddress.ip_network("64:ff9b::/96"):            # NAT64 well-known prefix
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if int(ip) >> 32 == 0:                                       # deprecated ::a.b.c.d
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def address_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast. Applied AFTER resolution."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return False
        v4 = _embedded_v4(ip)
        if v4 is not None:
            return address_allowed(v4)
        if ip in ipaddress.ip_network("64:ff9b:1::/48"):       # local-use NAT64
            return False
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_private or ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return False                                               # CGNAT (is_global covers in 3.12)
    return ip.is_global


def parse_and_check_syntax(url: str, allow_http: bool = True) -> tuple[str, str, int]:
    if len(url) > MAX_URL_LEN or any(c in url for c in "\r\n\t\x00 \\"):
        raise Denied("bad characters/length")
    p = urlsplit(url)
    scheme = p.scheme.lower()
    if scheme not in ALLOWED_SCHEMES or (scheme == "http" and not allow_http):
        raise Denied(f"scheme {scheme!r}")
    if p.username is not None or p.password is not None or "@" in p.netloc:
        raise Denied("userinfo")
    host = p.hostname
    if not host:
        raise Denied("no host")
    try:
        port = p.port if p.port is not None else (443 if scheme == "https" else 80)
    except ValueError:
        raise Denied("bad port")
    if not (port in (80, 443) or 1024 <= port <= 65535):
        raise Denied(f"port {port}")
    host = host.lower()
    if host.endswith("."):
        raise Denied("trailing dot")                               # 'localhost.' etc.
    if p.netloc.startswith("[") or ":" in host:
        try:
            ip = ipaddress.IPv6Address(host)
        except ValueError:
            raise Denied("bad ipv6 literal")
        if not address_allowed(ip):
            raise Denied(f"ipv6 literal {ip}")
        return scheme, host, port
    if _DOTTED_QUAD.match(host):
        if not address_allowed(ipaddress.IPv4Address(host)):
            raise Denied(f"ipv4 literal {host}")
        return scheme, host, port
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise Denied("idna")
    if not _DNS_NAME.match(host):
        raise Denied(f"non-canonical host {host!r}")                # decimal/octal/hex forms
    if host in DENY_NAMES or host.endswith(DENY_SUFFIXES):
        raise Denied(f"local name {host}")
    return scheme, host, port


def resolve_and_check(host: str, port: int) -> str:
    """Resolve once; ALL answers must be allowed; return the address to connect to."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = []
    for fam, _, _, _, sa in infos:
        ip = ipaddress.ip_address(sa[0].split("%")[0])
        if not address_allowed(ip):
            raise Denied(f"resolves to forbidden {ip}")
        addrs.append(str(ip))
    if not addrs:
        raise Denied("no address")
    return addrs[0]          # caller connects to THIS ip, with SNI/Host = host


# ----------------------------------------------------------------- self-test
DENY = [
    "http://127.0.0.1/", "http://[::1]/", "http://0x7f000001/", "http://2130706433/",
    "http://0177.0.0.1/", "http://127.1/", "http://localhost/", "http://localhost./",
    "http://LOCALHOST/", "http://foo.localhost/", "http://nas.local/", "http://169.254.169.254/",
    "http://[fe80::1%25eth0]/", "http://[fe80::1]/", "http://[fd00::1]/", "http://[fc00::1]/",
    "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[::127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[2002:c0a8:101::]/",
    "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/", "http://100.64.0.1/",
    "http://0.0.0.0/", "http://[::]/", "http://224.0.0.1/", "http://255.255.255.255/",
    "http://198.18.0.1/", "http://192.0.0.170/", "http://[ff02::1]/",
    "file:///etc/passwd", "concat:http://a|http://b", "subfile:,start,0,end,0,:http://x/",
    "data:video/mp4;base64,AAAA", "gopher://example.com/", "ftp://example.com/x.mkv",
    "rtmp://example.com/x", "tcp://example.com:80", "udp://example.com:1234", "crypto:http://x",
    "https://user:pass@example.com/", "https://example.com@127.0.0.1/", "https://example.com:22/",
    "https://example.com:0/", "https://example.com:99999/", "https://exa mple.com/",
    "https://example.com/\r\nX-Injected: 1", "https:///nohost", "//example.com/x",
    "https://[::1]:443/", "https://1.1.1.1.nip.io.:443/",
]
ALLOW_SYNTAX = [
    "https://store-031.weur.tb-cdn.st/dld/abc?token=x", "https://1.1.1.1/",
    "https://example.com:8443/x.mkv", "http://example.com/x.mkv", "https://[2606:4700::1111]/",
]

if __name__ == "__main__":
    bad = 0
    for u in DENY:
        try:
            s, h, pt = parse_and_check_syntax(u)
            resolve_and_check(h, pt) if h in ("localhost",) else None
            print(f"FAIL (allowed)  {u!r}"); bad += 1
        except Denied as e:
            print(f"ok   denied     {u!r:58} {e}")
        except OSError as e:
            print(f"ok   unresolv.  {u!r:58} {e}")
    for u in ALLOW_SYNTAX:
        try:
            parse_and_check_syntax(u)
            print(f"ok   allowed    {u!r}")
        except Denied as e:
            print(f"FAIL (denied)   {u!r:58} {e}"); bad += 1
    # Post-resolution check catches what syntax rules might miss (decimal forms on glibc/WinSock):
    for host in ("2130706433", "0x7f000001", "localhost"):
        try:
            resolve_and_check(host, 80); print(f"FAIL resolver allowed {host}"); bad += 1
        except (Denied, OSError) as e:
            print(f"ok   resolver   {host:58} {e}")
    print("\nRESULT:", "PASS" if not bad else f"{bad} FAILURES")
