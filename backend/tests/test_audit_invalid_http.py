"""Which kinds of traffic make uvicorn log 'Invalid HTTP request received.'? (audit of a production warning)"""
from __future__ import annotations

import logging
import socket
import ssl
import time

from helpers import free_port


def test_which_probes_produce_the_invalid_http_request_warning(live_factory, caplog):
    port = free_port()
    live_factory(None, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
    caplog.set_level(logging.DEBUG)

    def count():
        return sum("Invalid HTTP request" in r.getMessage() for r in caplog.records)

    def tls(send=None, alpn=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        if alpn: ctx.set_alpn_protocols(alpn)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s, ctx.wrap_socket(s) as t:
            if send: t.sendall(send)
            time.sleep(0.3)
    results = {}
    def run(name, fn):
        before = count()
        try:
            fn()
        except OSError:
            pass
        time.sleep(0.4)
        results[name] = count() - before
    def plain():
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n"); time.sleep(0.3)
    def raw_junk():
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"\x00\x01\x02garbage"); time.sleep(0.3)
    run("plaintext HTTP sent to the TLS port", plain)
    run("random bytes, no TLS", raw_junk)
    run("TLS ok, then non-HTTP bytes", lambda: tls(b"\x00\x01\x02junk\r\n\r\n"))
    run("TLS ok, then HTTP/2 preface (h2 client)", lambda: tls(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"))
    run("TLS ok, then a proper request", lambda: tls(b"GET /v1/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"))
    run("TLS ok, then nothing", lambda: tls())
    run("TLS ok, request with a bad request line", lambda: tls(b"BLAH\r\n\r\n"))
    print("\nINVALID-HTTP SOURCES " + str(results))
