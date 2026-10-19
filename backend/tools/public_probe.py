"""External-network probe for the OpenSeek PUBLIC guest endpoint. Run it from OUTSIDE your home network (phone hotspot,
a friend's PC, her house). Standard library only.

    python public_probe.py openseek.example.org:443 --pin "sha256//..."            # no secrets needed
    python public_probe.py openseek.example.org:443 --pin ... --token <guest token> # also exercises the authenticated guest API
    python public_probe.py openseek.example.org:443 --pin ... --closed-ports 8787,8788,3389,445

It checks, in this order: (1) the certificate pin; (2) the minimal health answer; (3) that every private route, method and
trick is refused; (4) that the owner pairing window is not reachable; (5) optionally the guest API; (6) that other ports on
the same address are not open. It sends only a handful of requests (the server blocks an address after 20 refused ones within 10 minutes, for 15
minutes; the probe stays well below that and says so if it was blocked). It never sends a token it was not given, and never prints one.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import socket
import ssl
import sys

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def rec(ok: bool, name: str, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def spki_pin(der: bytes) -> str:
    # SubjectPublicKeyInfo of an X.509 DER certificate, parsed minimally (no third-party packages).
    def tlv(b, i):
        tag = b[i]; n = b[i + 1]; j = i + 2
        if n & 0x80:
            k = n & 0x7F; n = int.from_bytes(b[j:j + k], "big"); j += k
        return tag, j, n
    _, j, n = tlv(der, 0)                      # Certificate
    _, j, n = tlv(der, j)                      # TBSCertificate
    i = j
    t, a, l = tlv(der, i)
    if t == 0xA0:                              # [0] version
        i = a + l
    for _ in range(5):                         # serial, signature, issuer, validity, subject
        t, a, l = tlv(der, i); i = a + l
    t, a, l = tlv(der, i)                      # subjectPublicKeyInfo
    spki = der[i:a + l]
    return "sha256//" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def request(host: str, port: int, method: str, path: str, body=None, token=None, headers=None, timeout=10):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE            # self-signed on purpose: identity is the pin, checked separately
    c = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, r.read(4096).decode("utf-8", "replace")
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("endpoint", help="host:port, e.g. openseek.example.org:443")
    ap.add_argument("--pin", required=True, help="the server's TLS pin (sha256//...), from the invitation or admin")
    ap.add_argument("--token", help="a GUEST device token, to also test the authenticated API")
    ap.add_argument("--closed-ports", default="8787,8788,3389,445,22", help="ports that must NOT answer on this address")
    a = ap.parse_args()
    host, _, port_s = a.endpoint.rpartition(":")
    port = int(port_s)

    # 1. TLS + pin
    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                got = spki_pin(tls.getpeercert(binary_form=True))
                rec(got == a.pin, "certificate pin matches", "" if got == a.pin else "got " + got)
                rec(tls.version() in ("TLSv1.2", "TLSv1.3"), "modern TLS", tls.version() or "")
    except OSError as e:
        rec(False, "TCP/TLS connect", type(e).__name__ + " - DNS, router port-forward, firewall or the server being down")
        return summary()

    # 2. health: reachable and minimal
    st, body = request(host, port, "GET", "/v1/health")
    try:
        keys = set(json.loads(body))
    except ValueError:
        keys = set()
    rec(st == 200 and keys == {"service", "api"}, "health is minimal", f"{st} keys={sorted(keys)}")
    if st == 429:
        rec(False, "probe not blocked", "this address was blocked by earlier refusals; lift with DELETE /admin/public/blocks on the PC")
        return summary()

    # 3. private routes (each refused request counts as a strike; the whole probe stays under the limit of 20)
    private = [("POST", "/v1/previews", {"source": {"url": "https://example.com/x.mkv"}}), ("GET", "/admin/status", None),
               ("GET", "/openapi.json", None), ("PUT", "/admin/sharing", {"enabled": True}),
               ("POST", "/v1/previews/" + "0" * 32 + "/priority", {}), ("POST", "/v1/previews/" + "0" * 32 + "/alt-source", {}),
               ("OPTIONS", "/v1/lookup", None), ("GET", "/v1/health/../admin/status", None)]
    leaked = []
    for m, p, b in private:
        try:
            s, t = request(host, port, m, p, b)
        except (OSError, http.client.HTTPException):
            continue                                                         # connection refused = also inaccessible
        if s not in (401, 403, 404, 405, 429) or '"service"' in t:
            leaked.append(f"{m} {p} -> {s}")
    rec(not leaked, "private routes and methods are refused", "; ".join(leaked))

    # 4. the owner's pairing window / anonymous API
    s, t = request(host, port, "POST", "/v1/pair/start", {})
    rec(s in (409, 429), "owner pairing window is not reachable", str(s))
    s, t = request(host, port, "POST", "/v1/lookup", {"imdb_id": "tt0111161", "duration_ms": 600000})
    rec(s in (401, 429), "lookup needs a guest token", str(s))

    # 5. optional: authenticated guest
    if a.token:
        s, t = request(host, port, "GET", "/v1/pair/status", token=a.token)
        rec(s == 200, "guest token works", str(s))
        s, t = request(host, port, "POST", "/v1/lookup", {"imdb_id": "tt0111161", "duration_ms": 600000}, token=a.token)
        rec(s in (200, 404), "guest lookup answers (200 match / 404 none)", str(s))
        s, t = request(host, port, "POST", "/v1/previews", {"source": {"url": "https://example.com/x.mkv"}, "layout": "tiles"}, token=a.token)
        rec(s in (403, 404), "guest cannot start extraction", str(s))
        s, t = request(host, port, "GET", "/admin/status", token=a.token)
        rec(s in (403, 404), "guest cannot reach admin", str(s))

    # 6. nothing else is open on this address
    for p in [int(x) for x in a.closed_ports.split(",") if x.strip()]:
        if p == port:
            continue
        sock = socket.socket(); sock.settimeout(4)
        try:
            opened = sock.connect_ex((host, p)) == 0
        except OSError:
            opened = False
        finally:
            sock.close()
        rec(not opened, f"port {p} is closed from outside")
    return summary()


def summary() -> int:
    bad = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
