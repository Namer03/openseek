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
def _admin_call(srv, method, path, token=None, headers=None, **kw):
    import httpx
    h = {"X-OpenSeek-Admin": token} if token is not None else {}
    h.update(headers or {})
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


# ---- fail-closed behaviour for deployments where the peer address cannot be trusted
PROXY_HDRS = [{"X-Forwarded-For": "203.0.113.9"}, {"Forwarded": "for=203.0.113.9"}, {"X-Real-IP": "203.0.113.9"},
              {"CF-Connecting-IP": "203.0.113.9"}, {"X-Forwarded-Host": "example.org"}, {"Via": "1.1 caddy"}]


def test_a_proxied_request_is_refused_on_the_owner_api(live_factory):
    """A reverse proxy or tunnel makes every client look like the proxy's (private) address. Anything that carries proxy headers is
    refused on the owner API, even with a valid token; the same request without the header works."""
    import httpx
    srv = live_factory(None)
    tok, _ = _pair_owner(srv)
    base = f"https://127.0.0.1:{srv.port}"
    with httpx.Client(verify=False, timeout=10) as c:
        assert c.get(f"{base}/v1/health").status_code == 200
        assert c.get(f"{base}/v1/pair/status", headers={"Authorization": "Bearer " + tok}).status_code == 200
        for h in PROXY_HDRS:
            for path, auth in (("/v1/health", {}), ("/v1/pair/status", {"Authorization": "Bearer " + tok})):
                r = c.get(base + path, headers={**h, **auth})
                assert r.status_code == 403 and r.json() == {"error": "proxy_refused"}, (h, path, r.status_code)


def test_a_proxied_request_is_refused_on_the_admin_api(live_factory):
    from helpers import ADMIN_TOKEN
    srv = live_factory(None)
    for h in PROXY_HDRS:
        r = _admin_call(srv, "GET", "/admin/status", token=ADMIN_TOKEN, headers=h)
        assert r.status_code == 403 and r.json() == {"error": "proxy_refused"}, h
    assert _admin_call(srv, "GET", "/admin/status", token=ADMIN_TOKEN).status_code == 200


def test_allow_public_clients_needs_an_explicit_acknowledgement():
    from openseek.config import ConfigError, Settings
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENSEEK_ALLOW_PUBLIC_CLIENTS": "1"})
    s = Settings.from_env({"OPENSEEK_ALLOW_PUBLIC_CLIENTS": "1", "OPENSEEK_ALLOW_PUBLIC_CLIENTS_ACK": "owner-api-is-public"})
    assert s.allow_public_clients is True
    assert Settings.from_env({}).allow_public_clients is False


def test_a_data_directory_writable_by_other_users_is_refused(monkeypatch, tmp_path):
    from openseek import hardening
    monkeypatch.delenv("OPENSEEK_ALLOW_SHARED_DATA_DIR", raising=False)
    monkeypatch.setattr(hardening, "data_dir_writable_by_others", lambda p: [r"BUILTIN\Users"])
    with pytest.raises(SystemExit) as e:
        hardening.refuse_shared_data_dir(tmp_path / "does-not-exist-yet")
    assert "refusing to start" in str(e.value)
    monkeypatch.setenv("OPENSEEK_ALLOW_SHARED_DATA_DIR", "1")                       # explicit, informed opt-out
    hardening.refuse_shared_data_dir(tmp_path)
    monkeypatch.delenv("OPENSEEK_ALLOW_SHARED_DATA_DIR")
    monkeypatch.setattr(hardening, "data_dir_writable_by_others", lambda p: [])
    hardening.refuse_shared_data_dir(tmp_path)                                      # a private folder starts normally


def test_read_only_exposure_warns_but_write_exposure_refuses():
    from openseek.hardening import broad_acl_principals
    ro = r"D:\x BUILTIN\Users:(OI)(CI)(RX)"
    rw = r"D:\x NT AUTHORITY\Authenticated Users:(OI)(CI)(M)"
    assert broad_acl_principals(ro) == [r"BUILTIN\Users"] and broad_acl_principals(ro, writes_only=True) == []
    assert broad_acl_principals(rw, writes_only=True) == [r"NT AUTHORITY\Authenticated Users"]


def _pair_owner(srv):
    """Full OSP1 pairing against a live test server; returns (token, device_id)."""
    return _register_owner(srv)


def _register_owner(srv):
    from test_scenarios import _register
    return _register(srv, "owner-tv")


@pytest.mark.parametrize("value", ["127.0.0.1", "192.168.1.20", "10.0.0.5", "172.16.4.9", "::1", "fd00::5", "192.168.1.20,127.0.0.1", "host-network"])
def test_a_container_may_be_published_on_loopback_or_a_private_lan_address(value):
    from openseek.hardening import published_on_error
    assert published_on_error(value) is None


@pytest.mark.parametrize("value", [None, "", "  ", "0.0.0.0", "::", "8.8.8.8", "203.0.113.7", "169.254.1.1", "100.64.0.5", "224.0.0.1",
                                   "localhost", "192.168.1.20,0.0.0.0", "192.168.1.20,8.8.8.8", "not-an-ip"])
def test_a_container_refuses_to_start_when_the_publish_address_is_missing_or_dangerous(value):
    from openseek.hardening import published_on_error
    assert published_on_error(value)                                     # a non-empty reason means: refuse


def test_the_compose_file_cannot_collide_with_another_deployment():
    """A fixed project name and image tag: cloning into a folder called 'openseek' must not adopt or recreate another compose project's
    container/volume, and the build must not overwrite another deployment's 'openseek:local'."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    lines = [ln.split("#")[0].strip() for ln in compose.splitlines()]
    assert "name: openseek-selfhosted" in lines
    assert "image: openseek-selfhosted:local" in lines
    assert "image: openseek:local" not in lines
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "openseek:local" not in docker
    assert any(ln.startswith("FROM python:") and "@sha256:" in ln for ln in docker.splitlines())
