"""Privacy audit: what an on-path observer (an ISP) can read. A recording TCP relay sits between real clients and the real
listeners of an ISOLATED server (same code and TLS settings as production); the bytes it records are exactly what crosses
the wire. Nothing here touches the production server."""
from __future__ import annotations

import http.server
import json
import socket
import ssl
import struct
import threading
import time
from collections import Counter

import httpx
import pytest

from helpers import free_port, wait_for
from origin import Origin
from openseek import pairing
from test_integration import _create, _policy, _status, _url
from test_nuvio_client_protocol import cli, kv, pair_with_client  # noqa: F401  (cli is a fixture)
from test_scenarios import _register
from test_sharing import ALT, ADMIN, HINTS, make_invite, needs_fixture, sharing_on


class Relay:
    """Plain TCP relay that records every byte in both directions (this is a wiretap, not a TLS terminator)."""

    def __init__(self, target_port: int):
        self.target = target_port
        self.log: list[tuple[float, str, bytes]] = []
        self.conns = 0
        self.lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.conns += 1
                cid = self.conns
            try:
                u = socket.create_connection(("127.0.0.1", self.target), timeout=10)
            except OSError:
                c.close()
                continue
            for a, b, d in ((c, u, f"c2s#{cid}"), (u, c, f"s2c#{cid}")):
                threading.Thread(target=self._pump, args=(a, b, d), daemon=True).start()

    def _pump(self, a, b, d):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                with self.lock:
                    self.log.append((time.time(), d, data))
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def blob(self, direction: str | None = None) -> bytes:
        with self.lock:
            return b"".join(d for _, tag, d in self.log if direction is None or tag.startswith(direction))

    def stop(self):
        self.sock.close()


def sni_of(client_hello: bytes) -> str | None:
    """Server name from the first TLS record (ClientHello) - the part of a TLS connection that is NOT encrypted."""
    try:
        if client_hello[0] != 22:
            return None
        i = 5 + 4 + 2 + 32
        i += 1 + client_hello[i]
        i += 2 + struct.unpack(">H", client_hello[i:i + 2])[0]
        i += 1 + client_hello[i]
        end = i + 2 + struct.unpack(">H", client_hello[i:i + 2])[0]
        i += 2
        while i < end:
            t, ln = struct.unpack(">HH", client_hello[i:i + 4])
            if t == 0:
                return client_hello[i + 9:i + 4 + ln].decode()
            i += 4 + ln
    except (IndexError, struct.error):
        return None
    return None


def first_flight(relay: Relay) -> list[bytes]:
    with relay.lock:
        return [d for _, tag, d in relay.log if tag.startswith("c2s") and d[:1] == b"\x16"]


# ---------------------------------------------------------------------------------------------- the wire test
@needs_fixture
def test_nothing_sensitive_crosses_the_wire_in_clear_and_what_remains_visible_is_only_metadata(live_factory, cli):
    o = Origin(ALT).start()
    try:
        pub_port = free_port()
        host = "openseek.example.org"
        srv = live_factory(_policy(o), pregen="full", workers_per_source=2, create_wait_s=10.0, public_port=pub_port,
                           public_bind="127.0.0.1", public_host="PLACEHOLDER:443")
        relay_pub, relay_api = Relay(pub_port), Relay(srv.port)
        srv.settings = srv.settings.with_(public_host=f"127.0.0.1:{relay_pub.port}")      # URLs the server hands out go through the wiretap
        srv.core.settings = srv.settings
        otok, _ = _register(srv, "owner-tv")
        source_url = _url(o, "ok_10min.mkv")

        # PATH C (owner): Nuvio -> OpenSeek over the LAN API, through the wiretap. The video URL and ids travel here.
        with httpx.Client(base_url=f"https://127.0.0.1:{relay_api.port}", verify=False, timeout=60, headers={"Authorization": "Bearer " + otok}) as owner, \
                srv.admin_client() as adm:
            sharing_on(adm)
            body = _create(owner, source_url, at_ms=0, layout="tiles", hints=HINTS)
            wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
            _create(owner, source_url, at_ms=0, layout="tiles", hints=HINTS)
            cache_ms = srv.core.cache.entry(next(iter(srv.core.jobs.sources.values())).fp).duration_ms

            # PATH B (guest, internet): pairing by invitation, lookup, VTT and JPEG, through the wiretap
            inv = make_invite(adm, public=True, label="audit")
            a = pair_with_client(srv, cli, inv["join"], "gf-tv", lambda: httpx.Client(base_url=f"https://127.0.0.1:{relay_pub.port}", verify=False, timeout=60))
            assert a["role"] == "guest"
            with httpx.Client(base_url=f"https://127.0.0.1:{relay_pub.port}", verify=False, timeout=60, headers={"Authorization": "Bearer " + a["token"]}) as g:
                look = g.post("/v1/lookup", json={"imdb_id": HINTS["imdb_id"], "duration_ms": cache_ms + 7000, "layout": "tiles"})
                assert look.status_code == 200
                cand = look.json()["candidates"][0]
                vtt = httpx.get(cand["vtt_url"], verify=False, timeout=60)
                assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")
                tile = next(l for l in vtt.text.splitlines() if l.startswith("https://")).split("#")[0]
                jpg = httpx.get(tile, verify=False, timeout=60)
                assert jpg.content[:2] == bytes([0xFF, 0xD8])
                cap = cand["vtt_url"].split("/v1/c/")[1].split("/")[0]
            time.sleep(0.5)

        secrets = {
            "IMDb id": HINTS["imdb_id"].encode(), "video URL path": b"ok_10min", "video URL host:port": source_url.split("//")[1].split("/")[0].encode(),
            "owner token": otok.encode(), "guest token": a["token"].encode(), "invite secret": inv["secret"].encode(),
            "invite code": inv["code"].replace("-", "").encode(), "join string": b"openseek://", "capability": cap.encode(),
            "WEBVTT body": b"WEBVTT", "JPEG magic": b"\xff\xd8\xff", "HTTP verb line": b"POST /v1", "Authorization header": b"uthorization",
            "preview id": body["preview_id"].encode(), "json key imdb_id": b"imdb_id", "device name": b"gf-tv",
        }
        report = {}
        for label_, rel in (("guest path (public listener)", relay_pub), ("owner path (LAN API)", relay_api)):
            blob = rel.blob()
            assert len(blob) > 10_000, "the capture is suspiciously small"
            found = [k for k, v in secrets.items() if v in blob]
            report[label_] = {"bytes": len(blob), "connections": rel.conns, "plaintext markers found": found}
            assert found == [], (label_, found)

        # what IS visible: the unencrypted first TLS flight (SNI) and sizes/timing
        hellos = first_flight(relay_pub)
        assert hellos
        report["SNI sent in clear by the client"] = sni_of(hellos[0])
        # certificate: TLS 1.3 encrypts it; check the server's bytes do not contain the cert subject
        s2c = relay_pub.blob("s2c")
        report["server certificate subject visible on the wire"] = b"openseek" in s2c
        sizes = Counter(len(d) for _, tag, d in relay_pub.log if tag.startswith("s2c"))
        report["largest server->client records (bytes)"] = sorted(sizes, reverse=True)[:3]
        print("\nWIRE REPORT " + json.dumps(report, indent=1))

        # POSITIVE CONTROL: the scanner really does find the same markers in plaintext HTTP (otherwise the test proves nothing)
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(b"WEBVTT\n" + HINTS["imdb_id"].encode() + b"\xff\xd8\xff" + a["token"].encode())
            def log_message(self, *a): pass
        hs = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=hs.serve_forever, daemon=True).start()
        ctl = Relay(hs.server_address[1])
        httpx.get(f"http://127.0.0.1:{ctl.port}/v1/lookup", headers={"Authorization": "Bearer " + a["token"]}, timeout=10)
        time.sleep(0.3)
        cb = ctl.blob()
        assert all(secrets[k] in cb for k in ("IMDb id", "guest token", "WEBVTT body", "JPEG magic", "Authorization header"))
        hs.shutdown()
        # second control: forcing TLS 1.2 makes the server certificate visible (it is encrypted only in TLS 1.3)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        r12 = Relay(pub_port)
        with socket.create_connection(("127.0.0.1", r12.port), timeout=5) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            assert t.version() == "TLSv1.2"
        time.sleep(0.3)
        assert b"openseek" in r12.blob("s2c")
        for r in (relay_pub, relay_api, ctl, r12):
            r.stop()
    finally:
        o.stop()


def test_protocol_versions_and_ciphers_on_the_public_listener(live_factory):
    port = free_port()
    srv = live_factory(None, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")

    def hs(minv, maxv):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version, ctx.maximum_version = minv, maxv
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s, ctx.wrap_socket(s) as t:
                return t.version(), t.cipher()[0]
        except (ssl.SSLError, OSError):
            return None
    T = ssl.TLSVersion
    v12, v13 = hs(T.TLSv1_2, T.TLSv1_2), hs(T.TLSv1_3, T.TLSv1_3)
    assert v12 and v13
    assert "GCM" in v12[1] or "CHACHA" in v12[1]          # an AEAD suite is what a modern client negotiates
    # plaintext HTTP on the TLS port gets no content and no redirect
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
        s.settimeout(3)
        try:
            assert s.recv(100) in (b"", )
        except (ConnectionResetError, socket.timeout):
            pass
