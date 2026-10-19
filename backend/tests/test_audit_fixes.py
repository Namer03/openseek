"""Regression tests for vulnerabilities found by the pre-release adversarial review."""
from __future__ import annotations

import pytest

from openseek.db import DB
from openseek.fetch import MAX_SOURCE_SIZE, _parse_cr
from openseek.library import Library


def _library(tmp_path):
    db = DB(tmp_path / "x.db")
    db.set_setting("metadata_index", "1")
    return db, Library(db, cache=None)


def _meta(db, fp):
    return db.q("SELECT imdb_id, tmdb_id, season, episode FROM release_meta WHERE fp=?", (fp,))


def test_a_contribution_cannot_relabel_an_existing_entry(tmp_path):
    """The owner's entry for a file is labelled; a guest who streams the same bytes must not rewrite the ids."""
    db, lib = _library(tmp_path)
    fp = "ab" * 32
    assert lib.record(fp, {"imdb_id": "tt0000001", "season": 0, "episode": 0}, 123)
    # contributed job: first writer wins
    lib.record(fp, {"imdb_id": "tt9999999", "season": 0, "episode": 0}, 123, replace=False)
    assert _meta(db, fp) == [("tt0000001", None, 0, 0)]


def test_a_contribution_still_labels_a_new_entry(tmp_path):
    db, lib = _library(tmp_path)
    fp = "cd" * 32
    lib.record(fp, {"imdb_id": "tt0000002", "season": 1, "episode": 2}, 5, replace=False)
    assert _meta(db, fp) == [("tt0000002", None, 1, 2)]


def test_the_owner_can_still_correct_a_label(tmp_path):
    db, lib = _library(tmp_path)
    fp = "ef" * 32
    lib.record(fp, {"imdb_id": "tt0000003", "season": 0, "episode": 0}, 5, replace=False)       # a guest's (maybe wrong) label
    lib.record(fp, {"imdb_id": "tt0000004", "season": 0, "episode": 0}, 5)                        # the owner's own job
    assert _meta(db, fp) == [("tt0000004", None, 0, 0)]


@pytest.mark.parametrize("total", [2 ** 64, 2 ** 64 + 5, MAX_SOURCE_SIZE + 1, 10 ** 30])
def test_an_absurd_content_range_total_is_rejected(total):
    assert _parse_cr(f"bytes 0-99/{total}") is None


def test_a_normal_content_range_is_still_accepted():
    assert _parse_cr("bytes 0-99/10000000000") == (0, 99, 10_000_000_000)


def test_a_slow_drip_origin_cannot_pin_a_preflight_thread(monkeypatch):
    """An origin that answers 206 and then sends one byte every 50 ms must be abandoned, not waited for."""
    import socket
    import threading
    import time
    from helpers import make_policy
    from openseek import fetch
    from openseek.fetch import Budget, Upstream, UpstreamError

    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(4)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            try:
                c.recv(65536)
                c.sendall(b"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-262143/10000000\r\n"
                          b"Content-Length: 262144\r\nContent-Type: video/x-matroska\r\n\r\n")
                while not stop.is_set():
                    c.sendall(b"x")
                    time.sleep(0.05)
            except OSError:
                pass
            finally:
                c.close()

    t = threading.Thread(target=serve, daemon=True); t.start()
    monkeypatch.setattr(fetch, "SLOW_GRACE_S", 1.0)
    monkeypatch.setattr(fetch, "MIN_RATE_BPS", 1_000_000)          # allowance: 1 s + 0.26 s
    class O:                                                         # make_policy only needs a .port
        port = srv.getsockname()[1]
    try:
        up = Upstream(make_policy([O]), f"http://127.0.0.1:{O.port}/f.mkv", {}, Budget(1 << 30))
        t0 = time.monotonic()
        with pytest.raises(UpstreamError):
            up.preflight()
        assert time.monotonic() - t0 < 8.0                           # without the fix this never returns (262144 bytes at 20 B/s)
        up.close()
    finally:
        stop.set(); srv.close()


def test_inbound_tls_context_refuses_anything_below_tls_1_2():
    """uvicorn builds the listener context from ssl.PROTOCOL_TLS_SERVER; on every supported Python (>=3.12) that has a TLS 1.2 floor.
    This pins that fact so a Python/uvicorn change that lowered it would fail the suite."""
    import ssl
    import uvicorn

    cfg = uvicorn.Config(lambda *a: None, ssl_certfile=None, ssl_keyfile=None)
    ctx = ssl.SSLContext(cfg.ssl_version)                                 # the protocol uvicorn would use for the listeners
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.minimum_version != ssl.TLSVersion.MINIMUM_SUPPORTED


# ---- the loopback admin API needs a per-install secret (review finding: any local process could pair itself as owner)
def _admin_call(srv, method, path, token=None, **kw):
    import httpx
    h = {"X-OpenSeek-Admin": token} if token is not None else {}
    return httpx.request(method, f"{srv.admin}{path}", headers=h, timeout=10, **kw)


def test_admin_api_refuses_every_call_without_the_secret(live_factory):
    srv = live_factory(None)
    for method, path in (("GET", "/admin/status"), ("GET", "/admin/devices"), ("POST", "/admin/pair"),
                         ("PUT", "/admin/sharing"), ("DELETE", "/admin/pair")):
        r = _admin_call(srv, method, path)
        assert r.status_code == 403 and r.json() == {"error": "admin_auth"}, (method, path, r.status_code)
    # the old constant header must not work either
    assert _admin_call(srv, "POST", "/admin/pair", token="1").status_code == 403
    assert _admin_call(srv, "GET", "/admin/status", token="1").status_code == 403


def test_admin_api_accepts_the_right_secret_and_rejects_near_misses(live_factory):
    from helpers import ADMIN_TOKEN
    srv = live_factory(None)
    assert _admin_call(srv, "GET", "/admin/status", token=ADMIN_TOKEN).status_code == 200
    for bad in (ADMIN_TOKEN[:-1], ADMIN_TOKEN + "x", ADMIN_TOKEN.upper(), ""):
        assert _admin_call(srv, "GET", "/admin/status", token=bad).status_code == 403, repr(bad)


def test_the_dashboard_shell_carries_no_secret_and_no_data(live_factory):
    from helpers import ADMIN_TOKEN
    srv = live_factory(None)
    r = _admin_call(srv, "GET", "/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert ADMIN_TOKEN not in r.text and "spki" not in r.text.lower().replace("spki_pin", "")


def test_a_generated_secret_is_stored_owner_only_and_is_stable(tmp_path):
    import os
    from openseek.auth import admin_token
    t1 = admin_token(tmp_path)
    assert len(t1) == 64 and admin_token(tmp_path) == t1
    if os.name == "posix":
        assert (tmp_path / "admin.token").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        admin_token(tmp_path, "short")
    assert admin_token(tmp_path, "x" * 16) == "x" * 16


# ---- start-up warnings for deployments the server cannot protect itself
ICACLS_SHARED = r"""D:\openseek-data NT AUTHORITY\SYSTEM:(OI)(CI)(F)
                  BUILTIN\Administrators:(OI)(CI)(F)
                  BUILTIN\Users:(OI)(CI)(IO)(GR,GE)
                  NT AUTHORITY\Authenticated Users:(OI)(CI)(IO)(M)
"""
ICACLS_PRIVATE = r"""C:\Users\someone\AppData\Local\OpenSeek\data NT AUTHORITY\SYSTEM:(OI)(CI)(F)
                  BUILTIN\Administrators:(OI)(CI)(F)
                  DESKTOP\someone:(OI)(CI)(F)
"""


def test_a_data_directory_open_to_other_local_users_is_flagged():
    from openseek.hardening import broad_acl_principals
    assert broad_acl_principals(ICACLS_SHARED) == [r"BUILTIN\Users", r"NT AUTHORITY\Authenticated Users"]


def test_a_private_data_directory_is_not_flagged():
    from openseek.hardening import broad_acl_principals
    assert broad_acl_principals(ICACLS_PRIVATE) == []
    assert broad_acl_principals("Everyone:(OI)(CI)(DENY)(R)") == []                     # a deny entry is not exposure
    assert broad_acl_principals(r"BUILTIN\Users:(OI)(CI)(RX)") == [r"BUILTIN\Users"]


def test_container_detection(monkeypatch):
    from openseek import hardening
    monkeypatch.setattr(hardening.os.path, "exists", lambda p: p == "/.dockerenv")
    assert hardening.running_in_container() is True
    monkeypatch.setattr(hardening.os.path, "exists", lambda p: False)
    monkeypatch.setattr(hardening.Path, "read_text", lambda self, **kw: "0::/\n")
    assert hardening.running_in_container() is False
