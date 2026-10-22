"""OpenSeek Cloud: settings, storage signing, pairing, previews and request-driven extraction end to end.

The cloud app runs in-process (FastAPI TestClient) with an in-memory key-value store and object store, against
the same local origin and synthetic media the self-hosted tests use. A second `Cloud` sharing the same stores
plays a different serverless instance.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from helpers import FPS, decode_barcode, make_policy, parse_vtt
from openseek.cloud.app import create_cloud_app
from openseek.cloud.crypto import Keys
from openseek.cloud.kv import MemoryKV
from openseek.cloud.runtime import CLOUD_PIN, Cloud
from openseek.cloud.settings import CloudConfigError, CloudSettings
from openseek.cloud.storage import CloudinaryStorage, MemoryStorage, R2Storage
from openseek.pairing import ITERATIONS, client_proof, derive_key, server_proof

SECRET = "s" * 40
PIN = "owner-pin-1234"


def settings(**kw) -> CloudSettings:
    env = {"OPENSEEK_CLOUD_SECRET": SECRET, "OPENSEEK_ADMIN_PIN": PIN, "OPENSEEK_PREGEN": "overview",
           "OPENSEEK_POLL_WORK_S": "2", "OPENSEEK_PRIORITY_WORK_S": "6"}
    env.update(kw)
    return CloudSettings.from_env(env)


# ============================================================================ settings
def test_settings_required():
    with pytest.raises(CloudConfigError):
        CloudSettings.from_env({"OPENSEEK_ADMIN_PIN": PIN})
    with pytest.raises(CloudConfigError):
        CloudSettings.from_env({"OPENSEEK_CLOUD_SECRET": SECRET})
    s = settings()
    assert s.storage == "local" and not s.kv_configured


def test_settings_vercel_needs_cloud_services():
    base = {"OPENSEEK_CLOUD_SECRET": SECRET, "OPENSEEK_ADMIN_PIN": PIN, "VERCEL": "1"}
    with pytest.raises(CloudConfigError, match="Upstash"):
        CloudSettings.from_env(base)
    kv = {"KV_REST_API_URL": "https://x.upstash.io", "KV_REST_API_TOKEN": "t"}
    with pytest.raises(CloudConfigError, match="cloud storage"):
        CloudSettings.from_env({**base, **kv})
    s = CloudSettings.from_env({**base, **kv, "CLOUDINARY_URL": "cloudinary://key:sec%2Fret@demo"})
    assert s.storage == "cloudinary" and s.cloudinary.api_secret == "sec/ret" and s.cloudinary.cloud_name == "demo"
    r2 = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "b", "R2_SECRET_ACCESS_KEY": "c", "R2_BUCKET_NAME": "d"}
    s = CloudSettings.from_env({**base, **kv, **r2, "CLOUDINARY_URL": "cloudinary://k:s@demo"})
    assert s.storage == "r2"                         # both set: R2 is used unless OPENSEEK_STORAGE says otherwise
    s = CloudSettings.from_env({**base, **kv, **r2, "CLOUDINARY_URL": "cloudinary://k:s@demo",
                                "OPENSEEK_STORAGE": "cloudinary"})
    assert s.storage == "cloudinary"
    with pytest.raises(CloudConfigError):
        CloudSettings.from_env({**base, **kv, "R2_ACCOUNT_ID": "a"})


# ============================================================================ storage signing
def test_cloudinary_signature_matches_documented_example():
    # Example from Cloudinary's "Signature generation" documentation.
    from openseek.cloud.settings import CloudinaryConfig

    st = CloudinaryStorage(CloudinaryConfig("demo", "key", "abcd"))
    sig = st.signature({"eager": "w_400,h_300,c_pad|w_260,h_200,c_crop", "public_id": "sample_image",
                        "timestamp": "1315060510"})
    assert sig == "bfd09f95f331f558cbd1320e67aa8d488770583e"


def test_r2_sigv4_matches_botocore():
    botocore = pytest.importorskip("botocore")
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    from openseek.cloud.settings import R2Config

    import calendar

    secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    body = b"\xff\xd8\xffhello\xff\xd9"
    url = "https://acct.r2.cloudflarestorage.com/bucket/openseek/tiles/abc.jpg"
    req = AWSRequest(method="PUT", url=url, data=body,
                     headers={"content-type": "image/jpeg", "x-amz-content-sha256": hashlib.sha256(body).hexdigest()})
    S3SigV4Auth(Credentials("AKIDEXAMPLE", secret), "s3", "auto").add_auth(req)
    t = calendar.timegm(time.strptime(req.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ"))
    st = R2Storage(R2Config("acct", "AKIDEXAMPLE", secret, "bucket"), clock=lambda: t)
    ours = st._signed("PUT", "openseek/tiles/abc.jpg", body, {"content-type": "image/jpeg"})
    assert ours.full_url == url
    theirs = req.headers["Authorization"]
    assert ours.get_header("Authorization") == theirs


# ============================================================================ end to end
@pytest.fixture(params=["subprocess", "inprocess"])
def stack(request, origin):
    kv, store = MemoryKV(), MemoryStorage()
    s = settings()
    cloud = Cloud(s, kv=kv, storage=store, policy=make_policy([origin]), engine=request.param)
    client = TestClient(create_cloud_app(cloud), base_url="https://openseek.example")
    yield {"cloud": cloud, "client": client, "kv": kv, "store": store, "origin": origin, "settings": s,
           "engine": request.param}
    cloud.close()


def owner(client) -> str:
    r = client.post("/configure/api/login", json={"pin": PIN})
    assert r.status_code == 200
    return r.json()["csrf"]


def pair(client) -> tuple[str, str]:
    csrf = owner(client)
    r = client.post("/configure/api/pairing/open", json={}, headers={"X-OpenSeek-CSRF": csrf})
    assert r.status_code == 200
    code = r.json()["code"]
    assert client.get("/v1/health").json()["pairing_open"] is True
    st = client.post("/v1/pair/start", json={}).json()
    assert st["spki_pin"] == CLOUD_PIN and st["tls"] == "ca" and st["iterations"] == ITERATIONS
    k = derive_key(code, st["pair_id"], st["salt"], st["spki_pin"], st["iterations"])
    r = client.post("/v1/pair/claim", json={"pair_id": st["pair_id"], "device_name": "Living room TV",
                                             "client_proof": client_proof(k, st["pair_id"], "Living room TV")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server_proof"] == server_proof(k, st["pair_id"], body["token"])
    assert client.get("/v1/health").json()["pairing_open"] is False          # single use
    return body["device_id"], body["token"]


def test_pairing_wrong_code_burns_window(stack):
    c = stack["client"]
    csrf = owner(c)
    c.post("/configure/api/pairing/open", json={}, headers={"X-OpenSeek-CSRF": csrf})
    st = c.post("/v1/pair/start", json={}).json()
    k = derive_key("2222-2222", st["pair_id"], st["salt"], st["spki_pin"], st["iterations"])
    for _ in range(5):
        r = c.post("/v1/pair/claim", json={"pair_id": st["pair_id"], "device_name": "x",
                                            "client_proof": client_proof(k, st["pair_id"], "x")})
        if r.status_code == 429:
            break
        assert r.status_code == 403 and r.json() == {"error": "pairing_failed"}
    assert c.post("/v1/pair/start", json={}).status_code in (409, 429)


def test_owner_page_requires_pin_and_csrf(stack):
    c = stack["client"]
    assert c.get("/configure").status_code == 200
    assert c.get("/configure/api/state").status_code == 401
    assert c.post("/configure/api/login", json={"pin": "nope"}).status_code == 403
    csrf = owner(c)
    assert c.post("/configure/api/pairing/open", json={}).status_code == 403            # no CSRF header
    st = c.get("/configure/api/state").json()
    assert st["storage"] == "memory" and st["devices"] == []
    r = c.post("/configure/api/devices", json={"name": "Phone"}, headers={"X-OpenSeek-CSRF": csrf})
    tok = r.json()["token"]
    assert c.get("/v1/pair/status", headers={"Authorization": f"Bearer {tok}"}).json()["device_name"] == "Phone"


def wait_tile(c, auth, pid, k, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = c.get(f"/v1/previews/{pid}", headers=auth).json()
        cues = parse_vtt(c.get(body["vtt_url"]).text)
        for _, _, url in cues:
            if f"/tiles/{k}.jpg" in url:
                return body, url
        c.post(f"/v1/previews/{pid}/priority", json={"at_ms": k * 10_000, "playhead_ms": 0}, headers=auth)
    raise AssertionError(f"tile {k} not made")


def test_preview_end_to_end(stack):
    c, origin = stack["client"], stack["origin"]
    did, token = pair(c)
    auth = {"Authorization": f"Bearer {token}"}
    url = origin.url("/f/main.mkv")
    r = c.post("/v1/previews", json={"source": {"url": url, "headers": {"User-Agent": "TV", "Cookie": "x"}},
                                      "at_ms": 60_000, "playhead_ms": 0, "layout": "hybrid"}, headers=auth)
    assert r.status_code == 202, r.text
    body = r.json()
    pid = body["preview_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", pid)
    assert body["vtt_url"].startswith("https://openseek.example/v1/c/") and "layout=tiles" in body["vtt_url"]

    # Scrubbing to 60 s makes that tile (P0) and the VTT lists it with its real pts.
    body, tile_url = wait_tile(c, auth, pid, 6)
    assert body["source_duration_ms"] == 120_000 and body["progress"]["tiles_total"] == 12
    assert body["state"] in ("ready", "complete")
    m = re.search(r"[?&]t=(\d+)", tile_url)
    pts = int(m.group(1))
    assert abs(pts - 60_000) <= 5000
    img = c.get(tile_url.split("#")[0])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    frame = decode_barcode(img.content)
    assert frame is not None and abs(frame / FPS * 1000 - pts) < 1000 / FPS + 1

    # Same device + same content -> same id; the stored record never holds the link in clear.
    again = c.post("/v1/previews", json={"source": {"url": url}}, headers=auth).json()
    assert again["preview_id"] == pid
    dump = json.dumps(stack["kv"]._d, default=str)
    assert "main.mkv" not in dump and "127.0.0.1" not in dump and token not in dump

    # A forged or foreign capability is refused; a missing tile is 404.
    bad = re.sub(r"/v1/c/[^/]+/", "/v1/c/" + "A" * 32 + "/", tile_url.split("#")[0])
    assert c.get(bad).status_code == 403
    assert c.get(re.sub(r"/tiles/\d+\.jpg", "/tiles/999.jpg", tile_url.split("#")[0])).status_code == 404

    # Polls keep going until the 60 s overview is complete.
    deadline = time.time() + 90
    while time.time() < deadline:
        body = c.get(f"/v1/previews/{pid}", headers=auth).json()
        ks = {int(re.search(r"/tiles/(\d+)\.jpg", u).group(1)) for _, _, u in parse_vtt(c.get(body["vtt_url"]).text)}
        if {0, 6} <= ks:
            break
    assert {0, 6} <= ks

    # Another instance (empty process state, same stores) serves the tiles and keeps extracting.
    other = Cloud(stack["settings"], kv=stack["kv"], storage=stack["store"], policy=stack["cloud"].policy,
                  engine=stack["cloud"].engine_mode)
    try:
        c2 = TestClient(create_cloud_app(other), base_url="https://openseek.example")
        assert c2.get(tile_url.split("#")[0]).content == img.content
        r = c2.post(f"/v1/previews/{pid}/priority", json={"at_ms": 100_000, "playhead_ms": 0}, headers=auth)
        assert r.status_code == 200
        assert "10" in {re.search(r"/tiles/(\d+)\.jpg", u).group(1)
                        for _, _, u in parse_vtt(c2.get(body["vtt_url"]).text)}
    finally:
        other.close()

    assert stack["cloud"].engine_mode == stack["engine"]      # no silent fallback in tests

    # Revoked devices are signed out.
    csrf = owner(c)
    assert c.post(f"/configure/api/devices/{did}/revoke", json={}, headers={"X-OpenSeek-CSRF": csrf}).json()["ok"]
    assert c.get(f"/v1/previews/{pid}", headers=auth).status_code == 401


def test_bad_sources(stack):
    c, origin = stack["client"], stack["origin"]
    _, token = pair(c)
    auth = {"Authorization": f"Bearer {token}"}
    for path, code in (("/f/random.bin", "not_video"), ("/f/nocues.mkv", "no_index")):
        b = c.post("/v1/previews", json={"source": {"url": origin.url(path)}}, headers=auth).json()
        assert b["state"] == "failed" and b["error"] == code
        assert c.get(f"/v1/previews/{b['preview_id']}", headers=auth).json()["error"] == code
        r = c.post(f"/v1/previews/{b['preview_id']}/priority", json={"at_ms": 0}, headers=auth)
        assert r.status_code == 200 and r.json()["state"] == "failed"
        assert c.delete(f"/v1/previews/{b['preview_id']}", headers=auth).status_code == 204
    r = c.post("/v1/previews", json={"source": {"url": "http://10.1.2.3/movie.mkv"}}, headers=auth)
    assert r.status_code == 400 and r.json() == {"error": "source_forbidden"}
    r = c.post("/v1/previews", json={"source": {"url": "file:///etc/passwd"}}, headers=auth)
    assert r.status_code == 400 and r.json() == {"error": "source_forbidden"}
    assert c.post("/v1/previews", json={"source": {"url": origin.url("/f/main.mkv")}}).status_code == 401


def test_mp4_and_hdr(stack):
    c, origin = stack["client"], stack["origin"]
    _, token = pair(c)
    auth = {"Authorization": f"Bearer {token}"}
    for path in ("/f/main.mp4", "/f/hdr.mkv"):
        b = c.post("/v1/previews", json={"source": {"url": origin.url(path)}, "at_ms": 0}, headers=auth).json()
        _, url = wait_tile(c, auth, b["preview_id"], 0)
        assert c.get(url.split("#")[0]).content[:3] == b"\xff\xd8\xff"


class FakeUpstash:
    """Just enough of the Upstash REST protocol: POST / with a command array, POST /pipeline with an array of them."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        self.mem = MemoryKV()
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                if self.headers.get("Authorization") != "Bearer tok":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cmds = body if self.path == "/pipeline" else [body]
                outer.seen.extend(c[0] for c in cmds)
                res = [{"result": outer.run(c)} for c in cmds]
                data = json.dumps(res if self.path == "/pipeline" else res[0]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def run(self, c):
        m = self.mem
        op = c[0]
        if op == "PING":
            return "PONG"
        if op == "GET":
            return m.get(c[1])
        if op == "SET":
            ex = int(c[c.index("EX") + 1]) if "EX" in c else None
            return "OK" if m.set(c[1], c[2], ex=ex, nx="NX" in c) else None
        if op == "DEL":
            m.delete(*c[1:])
            return 1
        if op == "INCR":
            return m.incr(c[1], 60)
        if op == "EXPIRE":
            return 1
        if op == "HSET":
            m.hset(c[1], dict(zip(c[2::2], c[3::2])))
            return 1
        if op == "HGET":
            return m.hget(c[1], c[2])
        if op == "HGETALL":
            out = []
            for k, v in m.hgetall(c[1]).items():
                out += [k, v]
            return out
        if op == "HDEL":
            m.hdel(c[1], c[2])
            return 1
        if op == "HLEN":
            return m.hlen(c[1])
        raise AssertionError(op)


def test_upstash_client_protocol():
    from openseek.cloud.kv import UpstashKV

    f = FakeUpstash()
    try:
        kv = UpstashKV(f"http://127.0.0.1:{f.httpd.server_address[1]}", "tok")
        assert kv.ping()
        assert kv.set("a", "1", ex=30, nx=True) and not kv.set("a", "2", nx=True) and kv.get("a") == "1"
        assert kv.incr("n", 60) == 1 and kv.incr("n", 60) == 2
        kv.hset("h", {"x": "1", "y": "2"}, ex=60)
        assert kv.hgetall("h") == {"x": "1", "y": "2"} and kv.hget("h", "y") == "2" and kv.hlen("h") == 2
        kv.hdel("h", "x")
        kv.delete("a")
        assert kv.get("a") is None and kv.hgetall("h") == {"y": "2"}
        assert not UpstashKV(f"http://127.0.0.1:{f.httpd.server_address[1]}", "wrong").ping()
    finally:
        f.httpd.shutdown()


def test_keys_separate_purposes():
    k = Keys(SECRET.encode())
    blob = k.seal({"url": "https://x"}, "pv:a")
    assert k.unseal(blob, "pv:a") == {"url": "https://x"}
    assert k.unseal(blob, "pv:b") is None
    assert Keys(("t" * 40).encode()).unseal(blob, "pv:a") is None
    assert k.preview_id("d1", "f") != k.preview_id("d2", "f")
    sess = k.session_cookie()
    assert k.session_ok(sess) and not k.session_ok(sess[:-2] + "AA") and not k.session_ok(None)
