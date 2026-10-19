"""Outbound destination policy (SR-8, SR-9) and vetted connections.

Every outbound request goes through `DestinationPolicy.vet()`:
  1. syntax: scheme https (http only if allowed, and only ever to public addresses), no
     userinfo, allowed port, canonical host (no decimal/octal/hex IPv4 forms, no local names);
  2. resolve ONCE; every returned address must be global unicast (loopback, RFC1918, CGNAT,
     link-local, ULA, multicast, reserved, unspecified, embedded-v4 forms and metadata
     addresses are denied);
  3. the caller connects to the vetted IP itself (no second resolution) while SNI, certificate
     verification and the Host header use the original hostname.

Redirects are followed by the fetcher (fetch.py), which calls vet() on every hop.

The only relaxation is `TestOnlyAllowance`: a set of exact (ip, port) pairs that tests build in
Python code to reach their local fixture server. Nothing reads it from the environment or the
configuration, so a production process cannot enable it.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

MAX_URL_LEN = 4096
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DNS_NAME = re.compile(rf"^(?:{_LABEL}\.)+[a-z][a-z0-9-]{{0,62}}$")
_DOTTED_QUAD = re.compile(r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$")
DENY_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
DENY_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".intranet", ".corp")
_METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
             ipaddress.ip_address("100.100.100.200")}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")

# Request headers a client may ask us to forward (SR-11). Everything else is dropped.
FORWARDABLE_HEADERS = {"user-agent": "User-Agent", "referer": "Referer", "origin": "Origin"}
MAX_HEADER_VALUE = 1024


class Denied(ValueError):
    """Destination refused by policy. `reason` is a short internal code (never shown to clients)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [sa[0] for _, _, _, _, sa in infos]


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if int(ip) >> 32 == 0:  # deprecated IPv4-compatible ::a.b.c.d (and :: / ::1 handled below)
        if int(ip) in (0, 1):
            return None
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def address_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast."""
    if ip in _METADATA:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return False
        v4 = _embedded_v4(ip)
        if v4 is not None:
            return address_allowed(v4)
        if ip in _NAT64_LOCAL:
            return False
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_private or ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return ip.is_global


@dataclass(frozen=True)
class TestOnlyAllowance:
    """Exact (ip, port) pairs that bypass the address and port rules. TESTS ONLY.

    Constructed in test code and passed to DestinationPolicy(...). There is intentionally no
    configuration or environment path that creates one."""

    __test__ = False  # not a pytest class
    pairs: frozenset[tuple[str, int]]

    def __post_init__(self):
        for ip, port in self.pairs:
            ipaddress.ip_address(ip)
            if not 0 < int(port) < 65536:
                raise ValueError("bad port")

    def allows(self, ip: str, port: int) -> bool:
        return (str(ipaddress.ip_address(ip)), int(port)) in self.pairs


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str               # lowercase hostname or IP literal (no brackets)
    port: int
    path: str               # path + "?" + query (what goes on the request line)
    addrs: tuple[str, ...]  # vetted addresses, connect to these only

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.port)

    def url(self) -> str:
        h = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        hp = h if self.port == default else f"{h}:{self.port}"
        return f"{self.scheme}://{hp}{self.path}"


class DestinationPolicy:
    def __init__(self, allowed_ports: Iterable[int] = (80, 443, 8080, 8443), allow_http: bool = True,
                 resolver: Resolver | None = None, ssl_context: ssl.SSLContext | None = None,
                 allowance: TestOnlyAllowance | None = None):
        self.allowed_ports = frozenset(int(p) for p in allowed_ports)
        self.allow_http = allow_http
        self.resolver = resolver or system_resolver
        self.allowance = allowance
        if ssl_context is None:
            ssl_context = ssl.create_default_context()
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        # full verification is mandatory: refuse a context that would skip it
        if ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname:
            raise ValueError("outbound TLS context must verify certificates and hostnames")
        self.ssl_context = ssl_context

    # ---------------------------------------------------------------- syntax
    def check_syntax(self, url: str) -> tuple[str, str, int, str, bool]:
        """Return (scheme, host, port, path_query, is_ip_literal) or raise Denied."""
        if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
            raise Denied("length")
        if any(c in url for c in "\r\n\t\x00 \\") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
            raise Denied("characters")
        try:
            p = urlsplit(url)
        except ValueError:
            raise Denied("syntax") from None
        scheme = p.scheme.lower()
        if scheme not in ("https", "http") or (scheme == "http" and not self.allow_http):
            raise Denied("scheme")
        if p.username is not None or p.password is not None or "@" in p.netloc:
            raise Denied("userinfo")
        try:
            host = p.hostname
            port = p.port if p.port is not None else (443 if scheme == "https" else 80)
        except ValueError:
            raise Denied("port") from None
        if not host:
            raise Denied("host")
        host = host.lower()
        if host.endswith("."):
            raise Denied("trailing_dot")
        path = p.path or "/"
        if not path.startswith("/"):
            raise Denied("syntax")
        if p.query:
            path += "?" + p.query
        if p.netloc.startswith("[") or ":" in host:
            if "%" in host:
                raise Denied("scope_id")
            try:
                ip = ipaddress.IPv6Address(host)
            except ValueError:
                raise Denied("ipv6_literal") from None
            return scheme, str(ip), port, path, True
        if _DOTTED_QUAD.match(host):
            return scheme, host, port, path, True
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise Denied("idna") from None
        if not _DNS_NAME.match(host):
            raise Denied("non_canonical_host")
        if host in DENY_NAMES or host.endswith(DENY_SUFFIXES):
            raise Denied("local_name")
        return scheme, host, port, path, False

    def _addr_ok(self, ip: str, port: int) -> bool:
        if self.allowance is not None and self.allowance.allows(ip, port):
            return True
        if port not in self.allowed_ports:
            return False
        return address_allowed(ipaddress.ip_address(ip))

    # ---------------------------------------------------------------- vet
    def vet(self, url: str) -> Target:
        scheme, host, port, path, literal = self.check_syntax(url)
        if literal:
            addrs = [host]
        else:
            try:
                addrs = list(self.resolver(host, port))
            except (OSError, UnicodeError):
                raise Denied("resolve_failed") from None
            if not addrs:
                raise Denied("no_address")
        clean = []
        for a in addrs:
            try:
                ip = ipaddress.ip_address(str(a).split("%")[0])
            except ValueError:
                raise Denied("bad_address") from None
            if "%" in str(a):
                raise Denied("address")
            if not self._addr_ok(str(ip), port):
                # one bad answer poisons the whole set (DNS rebinding / split answers)
                raise Denied("port" if port not in self.allowed_ports and address_allowed(ip) else "address")
            clean.append(str(ip))
        return Target(scheme, host, port, path, tuple(dict.fromkeys(clean)))

    # ---------------------------------------------------------------- connect
    def connect(self, target: Target, timeout: float) -> http.client.HTTPConnection:
        """Open a connection to one of the vetted addresses. Raises OSError if none answers."""
        last: Exception | None = None
        for ip in target.addrs:
            if target.scheme == "https":
                conn: http.client.HTTPConnection = _VettedHTTPSConnection(
                    target.host, target.port, ip, timeout, self.ssl_context)
            else:
                conn = _VettedHTTPConnection(target.host, target.port, ip, timeout)
            try:
                conn.connect()
                return conn
            except (OSError, ssl.SSLError) as e:
                last = e
                conn.close()
        raise last if last else OSError("no address")


class _VettedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._vetted_ip = ip

    def connect(self):  # never resolves self.host
        self.sock = socket.create_connection((self._vetted_ip, self.port), self.timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class _VettedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._vetted_ip = ip
        self._ctx = context

    def connect(self):  # TCP to the vetted IP; SNI + certificate check against the hostname
        sock = socket.create_connection((self._vetted_ip, self.port), self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def filter_forwarded_headers(headers: dict | None) -> dict[str, str]:
    """Keep only User-Agent / Referer / Origin with sane values (SR-11)."""
    out: dict[str, str] = {}
    if not isinstance(headers, dict):
        return out
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        canon = FORWARDABLE_HEADERS.get(k.strip().lower())
        if canon is None:
            continue
        if len(v) > MAX_HEADER_VALUE or any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            continue
        out[canon] = v
    return out
