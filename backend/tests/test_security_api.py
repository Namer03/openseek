"""ABUSE-TESTS.md §1, §2, §3 (API level), §4, §5 against the real ASGI apps (httpx ASGITransport).
Only the remote origin is a local test server."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from helpers import make_policy
from openseek.app import Core, api_openapi, create_admin_app, create_api_app
from openseek.config import Settings
from openseek.pairing import client_proof, derive_key, new_token, token_hash
from test_netpolicy import ABUSE_MATRIX

BASE = "https://openseek.lan:8787"
LAN_CLIENT = ("192.168.1.50", 50000)
PID = "0" * 32


@pytest.fixture()
def core(tmp_path, origin):
    pol = make_policy([origin], {"media.example.com": ["127.0.0.1"], "evil.example.com": ["127.0.0.1"]})
    c = Core(Settings().with_(data_dir=tmp_path / "data", admin_port=8788, create_wait_s=5.0, admin_token="test-admin-token"), pol).start()
    yield c
    c.stop()


@pytest.fixture()
def token(core):
    tok = new_token()
    dev = core.auth.register("Shield", token_hash(tok))
    return tok, dev


def run(coro):
    return asyncio.run(coro)


async def req(core, method, path, token=None, client=LAN_CLIENT, admin=False, **kw):
    app = create_admin_app(core) if admin else create_api_app(core)
    base = "http://127.0.0.1:8788" if admin else BASE
    headers = kw.pop("headers", {})
    if admin:
        headers.setdefault("X-OpenSeek-Admin", "test-admin-token")      # every admin call needs the per-install secret
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url=base) as c:
        return await c.request(method, path, headers=headers, **kw)


def call(core, method, path, **kw) -> httpx.Response:
    return run(req(core, method, path, **kw))


def test_metrics_stays_on_guarded_admin_app(core):
    r = call(core, "GET", "/admin/metrics", admin=True, client=("127.0.0.1", 50000))
    assert r.status_code == 200
    assert r.json()["config"]["p2_pause_s"] == 5
    assert "sources" in r.json()
    assert call(core, "GET", "/admin/metrics").status_code == 401
    assert call(core, "GET", "/admin/metrics", admin=True,
                client=("127.0.0.1", 50000),
                headers={"Host": "attacker.example"}).status_code == 403


def _fill(path: str) -> str:
    return (path.replace("{preview_id}", PID).replace("{cap}", "A" * 32).replace("{name}", "1.jpg"))


# ---------------------------------------------------------------- §1 auth
def test_every_route_requires_auth(core):
    doc = api_openapi(core)
    seen = 0
    for path, ops in doc["paths"].items():
        for method in ops:
            r = call(core, method.upper(), _fill(path), json={})
            if path in ("/v1/health", "/v1/pair/start", "/v1/pair/claim"):
                continue
            seen += 1
            if path.startswith("/v1/c/"):
                assert r.status_code == 403 and r.json() == {"error": "forbidden"}, path
            else:
                assert r.status_code == 401 and r.json() == {"error": "unauthorized"}, (method, path)
    assert seen >= 8
    assert call(core, "GET", "/v1/not-a-route").status_code == 401   # unknown paths leak nothing


def test_bad_token_401(core, token):
    for h in (f"Bearer {new_token()}", "Bearer x", "Basic abc", f"bearer {token[0]}", ""):
        r = call(core, "GET", "/v1/pair/status", headers={"Authorization": h})
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert call(core, "GET", "/v1/pair/status", token=token[0]).json()["device_name"] == "Shield"


def test_revoked_token_401(core, token):
    assert call(core, "DELETE", f"/admin/devices/{token[1]}", admin=True, client=("127.0.0.1", 1),
                headers={"X-OpenSeek-Admin": "test-admin-token"}).status_code == 204
    assert call(core, "GET", "/v1/pair/status", token=token[0]).status_code == 401


def test_self_revoke(core, token):
    assert call(core, "DELETE", "/v1/pair", token=token[0]).status_code == 204
    assert call(core, "GET", "/v1/pair/status", token=token[0]).status_code == 401


def test_pairing_over_api(core):
    assert call(core, "POST", "/v1/pair/start", json={}).json() == {"error": "pairing_closed"}
    assert call(core, "POST", "/v1/pair/claim",
                json={"pair_id": "0" * 32, "device_name": "x", "client_proof": "0" * 64}).status_code == 403
    code = call(core, "POST", "/admin/pair", admin=True, client=("127.0.0.1", 1),
                headers={"X-OpenSeek-Admin": "test-admin-token"}).json()["code"]
    assert re.fullmatch(r"[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}", code)
    assert call(core, "GET", "/v1/health").json()["pairing_open"] is True
    st = call(core, "POST", "/v1/pair/start", json={}).json()
    assert st["iterations"] == 600_000 and st["spki_pin"] == core.spki_pin and len(st["salt"]) == 32
    k = derive_key(code, st["pair_id"], st["salt"], st["spki_pin"], st["iterations"])
    body = {"pair_id": st["pair_id"], "device_name": "TV", "client_proof": client_proof(k, st["pair_id"], "TV")}
    r = call(core, "POST", "/v1/pair/claim", json=body)
    assert r.status_code == 200 and set(r.json()) == {"device_id", "token", "server_proof", "role"} and r.json()["role"] == "owner"
    assert call(core, "POST", "/v1/pair/claim", json=body).json() == {"error": "pairing_failed"}   # replay
    tok = r.json()["token"]
    assert call(core, "GET", "/v1/pair/status", token=tok).status_code == 200
    raw = b"".join(p.read_bytes() for p in Path(core.data_dir).rglob("*") if p.is_file())
    assert tok.encode() not in raw and code.replace("-", "").encode() not in raw   # test_token_hash_only_on_disk


def test_health_shape(core):
    r = call(core, "GET", "/v1/health")
    assert r.status_code == 200
    assert set(r.json()) == {"service", "api", "version", "ffmpeg", "pairing_open"}
    assert r.json()["service"] == "openseek" and r.json()["api"] == 1


# ---------------------------------------------------------------- §2 caps (API level)
def _job(core, token, origin, name="main.mkv"):
    r = call(core, "POST", "/v1/previews", token=token,
             json={"source": {"url": f"http://media.example.com:{origin.port}/f/{name}"}, "at_ms": 0})
    assert r.status_code == 202, r.text
    return r.json()


def test_cap_checks(core, token, origin):
    tok, dev = token
    j = _job(core, tok, origin)
    vtt = j["vtt_url"]
    path = vtt.replace(BASE, "")
    assert call(core, "GET", path).status_code == 200
    cap = path.split("/")[3]
    exp = int(path.split("exp=")[1].split("&")[0])
    flipped = cap[:3] + ("A" if cap[3] != "A" else "B") + cap[4:]
    assert call(core, "GET", path.replace(cap, flipped)).status_code == 403                      # tampered
    assert call(core, "GET", path.replace(f"exp={exp}", f"exp={exp + 1}")).status_code == 403     # exp tampered
    past = exp - 7 * 3600
    old = core.caps.cap(dev, j["preview_id"], past)
    assert call(core, "GET", f"/v1/c/{old}/{j['preview_id']}/thumbs.vtt?exp={past}").status_code == 403  # expired
    j2 = _job(core, tok, origin, "main.mp4")
    assert call(core, "GET", path.replace(j["preview_id"], j2["preview_id"])).status_code == 403    # other job
    r = call(core, "GET", path.split("?")[0], token=tok)                                            # Bearer accepted
    assert r.status_code == 200 and r.text.startswith("WEBVTT")
    assert call(core, "GET", path.split("?")[0]).json() == {"error": "forbidden"}


# ---------------------------------------------------------------- §3 SSRF via the API
@pytest.mark.parametrize("url", ABUSE_MATRIX)
def test_ssrf_matrix_via_api(core, token, canary, url):
    url = url.replace("127.0.0.1/", f"127.0.0.1:{canary.port}/")
    r = call(core, "POST", "/v1/previews", token=token[0], json={"source": {"url": url}})
    assert r.status_code == 400 and r.json() == {"error": "source_forbidden"}
    assert canary.hits == 0


def test_redirect_to_loopback_blocked_via_api(core, token, origin, canary):
    origin.reset(redirects={"/r": f"http://127.0.0.1:{canary.port}/latest"})
    r = call(core, "POST", "/v1/previews", token=token[0],
             json={"source": {"url": f"http://media.example.com:{origin.port}/r"}})
    assert r.status_code == 202
    assert r.json()["state"] == "failed" and r.json()["error"] == "source_forbidden"
    assert canary.hits == 0


def test_hls_playlist_rejected(core, token, origin, canary):
    body = (f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\nhttp://127.0.0.1:{canary.port}/seg.ts\n"
            f"#EXT-X-ENDLIST\n").encode()
    origin.reset(raw={"/list.m3u8": (206, "application/vnd.apple.mpegurl", body)})
    # a 206 without Content-Range is not a valid range answer either; serve it as a proper range:
    origin.reset()
    (Path(origin.root) / "list.m3u8").write_bytes(body)
    r = call(core, "POST", "/v1/previews", token=token[0],
             json={"source": {"url": f"http://media.example.com:{origin.port}/f/list.m3u8"}})
    assert r.json()["state"] == "failed" and r.json()["error"] == "not_video"
    assert canary.hits == 0


# ---------------------------------------------------------------- §4 resource exhaustion
def test_oversize_body_413(core, token):
    big = {"source": {"url": "https://example.com/x.mkv"}, "pad": "x" * (1 << 20)}
    r = call(core, "POST", "/v1/previews", token=token[0], content=json.dumps(big),
             headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"error": "too_large"}
    r = call(core, "POST", "/v1/pair/claim", content=b"{" + b" " * 20000 + b"}",
             headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_bad_bodies_400_without_echo(core, token):
    marker = "MARKER-7f3a"
    for body in ({"source": {"url": 5}}, {"source": {}}, {"at_ms": -1, "source": {"url": "https://e.com/" + marker}},
                 {"source": {"url": "https://e.com/x", "duration_ms": 10 ** 12}},
                 {"source": {"url": "https://e.com/x"}, "layout": marker}, [marker]):
        r = call(core, "POST", "/v1/previews", token=token[0], json=body)
        assert r.status_code == 400 and r.json() == {"error": "bad_request"}, body
        assert marker not in r.text
    r = call(core, "POST", "/v1/previews", token=token[0], content=b"not json " + marker.encode(),
             headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and marker not in r.text


def test_duration_cap(core, token):
    r = call(core, "POST", "/v1/previews", token=token[0],
             json={"source": {"url": "https://example.com/x.mkv", "duration_ms": 10 ** 12}})
    assert r.status_code == 400


def test_jobs_reject_url_in_query(core, token, origin):
    r = call(core, "POST", f"/v1/previews?url=http://media.example.com:{origin.port}/f/main.mkv", token=token[0],
             json={"source": {"url": f"http://media.example.com:{origin.port}/f/main.mkv"}})
    assert r.status_code == 400 and origin.hits == 0


def test_job_flood_429(tmp_path, origin):
    pol = make_policy([origin], {"media.example.com": ["127.0.0.1"]})
    c = Core(Settings().with_(data_dir=tmp_path / "d", max_active_sources=1, pregen="full", admin_token="test-admin-token"), pol).start()
    try:
        toks = []
        for i in range(2):
            t = new_token()
            c.auth.register(f"tv{i}", token_hash(t))
            toks.append(t)
        r1 = call(c, "POST", "/v1/previews", token=toks[0],
                  json={"source": {"url": f"http://media.example.com:{origin.port}/f/main.mkv"}})
        assert r1.status_code == 202
        r2 = call(c, "POST", "/v1/previews", token=toks[1],
                  json={"source": {"url": f"http://media.example.com:{origin.port}/f/main.mp4"}})
        assert r2.status_code == 429 and r2.json() == {"error": "busy"} and "retry-after" in r2.headers
    finally:
        c.stop()


# ---------------------------------------------------------------- §5 storage, headers, client IPs
@pytest.mark.parametrize("bad", ["../../secret.key", "%2e%2e%2f%2e%2e", "A" * 32, "0" * 33, "0" * 31, "..",
                                 "0" * 31 + "/", "%00" + "0" * 29])
def test_path_traversal_ids(core, token, bad):
    tok = token[0]
    for m, p in (("GET", f"/v1/previews/{bad}"), ("POST", f"/v1/previews/{bad}/priority"),
                 ("DELETE", f"/v1/previews/{bad}")):
        r = call(core, m, p, token=tok, json={})
        assert r.status_code in (400, 404, 405), (p, r.status_code)
    for p in (f"/v1/c/{'A' * 32}/{bad}/thumbs.vtt", f"/v1/c/{bad}/{PID}/tiles/1.jpg",
              f"/v1/c/{'A' * 32}/{PID}/tiles/{bad}.jpg", f"/v1/c/{'A' * 32}/{PID}/sheets/{bad}",
              f"/v1/c/{'A' * 32}/{PID}/tiles/..%2f..%2fsecret.key"):
        r = call(core, "GET", p, token=tok)
        assert r.status_code in (403, 404), (p, r.status_code)
        assert r.headers["content-type"].startswith("application/json")


def test_security_headers(core, token):
    for r in (call(core, "GET", "/v1/health"), call(core, "GET", "/v1/pair/status"),
              call(core, "GET", "/v1/pair/status", token=token[0]),
              call(core, "GET", f"/v1/c/{'A' * 32}/{PID}/thumbs.vtt?exp=1")):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"
        assert "server" not in r.headers


def test_public_client_ip_refused(core, token, tmp_path, origin):
    for ip in ("8.8.8.8", "2001:4860:4860::8888", "100.64.1.1"):
        r = call(core, "GET", "/v1/health", client=(ip, 1))
        assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    r = call(core, "GET", "/v1/health", client=("8.8.8.8", 1), headers={"X-Forwarded-For": "192.168.1.2"})
    assert r.status_code == 403                                               # XFF ignored
    for ip in ("192.168.1.9", "10.1.2.3", "172.20.0.5", "127.0.0.1", "::1", "fd12::5", "fe80::1",
               "::ffff:192.168.1.9"):
        assert call(core, "GET", "/v1/health", client=(ip, 1)).status_code == 200, ip
    pol = make_policy([origin], {})
    c2 = Core(Settings().with_(data_dir=tmp_path / "pub", allow_public_clients=True, admin_token="test-admin-token"), pol).start()
    try:
        assert call(c2, "GET", "/v1/health", client=("8.8.8.8", 1)).status_code == 200
    finally:
        c2.stop()


def test_admin_guard(core):
    ok = dict(admin=True, client=("127.0.0.1", 1))
    assert call(core, "GET", "/admin/status", **ok).status_code == 200
    assert call(core, "POST", "/admin/pair", headers={"X-OpenSeek-Admin": ""}, **ok).json() == {"error": "admin_auth"}      # no secret
    assert call(core, "GET", "/admin/status", headers={"X-OpenSeek-Admin": "wrong-secret-value"}, **ok).json() == {"error": "admin_auth"}
    assert call(core, "POST", "/admin/pair", headers={"X-OpenSeek-Admin": "test-admin-token", "Host": "evil.example:8788"},
                **ok).json() == {"error": "bad_host"}                             # DNS rebinding
    assert call(core, "POST", "/admin/pair", headers={"X-OpenSeek-Admin": "test-admin-token", "Origin": "https://evil.example"},
                **ok).json() == {"error": "bad_origin"}
    assert call(core, "GET", "/admin/status", admin=True, client=("192.168.1.5", 1)).status_code == 403
    st = call(core, "GET", "/admin/status", **ok).json()
    assert st["server"]["spki_pin"] == core.spki_pin and st["pairing"]["open"] is False


def test_preview_status_and_priority_flow(core, token, origin):
    tok = token[0]
    j = _job(core, tok, origin)
    pid = j["preview_id"]
    assert set(j) == {"preview_id", "state", "error", "vtt_url", "vtt_version", "source_duration_ms",
                      "tile_interval_ms", "tolerance_ms", "progress", "refresh_after_ms"}
    r = call(core, "POST", f"/v1/previews/{pid}/priority", token=tok, json={"at_ms": 60_000, "playhead_ms": 1000})
    assert r.status_code == 200 and set(r.json()) == {"vtt_version", "state"}
    r = call(core, "GET", f"/v1/previews/{pid}?wait_version=0&wait_ms=2000", token=tok)
    assert r.status_code == 200 and r.json()["vtt_version"] >= 1
    other = new_token()
    core.auth.register("Other", token_hash(other))
    assert call(core, "GET", f"/v1/previews/{pid}", token=other).status_code == 404    # not your job
    assert call(core, "DELETE", f"/v1/previews/{pid}", token=tok).status_code == 204
    assert call(core, "GET", f"/v1/previews/{pid}", token=tok).status_code == 404
    assert call(core, "GET", j["vtt_url"].replace(BASE, "")).status_code == 200            # tiles stay served
