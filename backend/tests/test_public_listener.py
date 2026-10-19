"""The internet-facing guest listener: deny by default, guests only, per-client limits, borrowed capabilities."""
from __future__ import annotations

import json

import httpx
import pytest

from helpers import free_port, wait_for
from origin import Origin
from openseek import pairing
from openseek.config import ConfigError, Settings
from openseek.public import PublicLimiter, PublicLimits, classify
from test_integration import _create, _policy, _status, _url
from test_scenarios import _register
from test_sharing import ALT, ADMIN, HINTS, make_invite, needs_fixture, sharing_on

ID = "a" * 32


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    monkeypatch.setattr(pairing, "ITERATIONS", 200_000)


def pjoin(srv, invite_id: str, code: str, name: str = "far-tv", proof_code: str | None = None, headers=None) -> httpx.Response:
    """OSP1 through the PUBLIC listener."""
    plain = code.replace("-", "")
    secret = plain[4:] if len(plain) == 16 else plain
    with srv.public_client() as c:
        c.headers.update(headers or {})
        s = c.post("/v1/pair/start", json={"invite": invite_id})
        if s.status_code != 200:
            return s
        s = s.json()
        k = pairing.derive_key(proof_code or secret, s["pair_id"], s["salt"], s["spki_pin"], s["iterations"])
        r = c.post("/v1/pair/claim", json={"pair_id": s["pair_id"], "device_name": name,
                                           "client_proof": pairing.client_proof(k, s["pair_id"], name)})
        if r.status_code == 200:
            assert r.json()["server_proof"] == pairing.server_proof(k, s["pair_id"], r.json()["token"])
        return r


@pytest.fixture()
def pub(live_factory):
    """A live server WITH the public listener (bound to loopback for the test) and one cached title."""
    o = Origin(ALT).start() if ALT.exists() else None
    port = free_port()
    srv = live_factory(_policy(o) if o else None, pregen="full", workers_per_source=2, create_wait_s=10.0,
                       public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
    srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")},
                                                         strikes=10 ** 6))      # the matrix below probes far more than a person would
    yield srv, o
    if o:
        o.stop()


# ------------------------------------------------------------------------------------------ configuration
def test_the_public_listener_is_off_by_default_and_misconfiguration_is_refused(live_factory):
    assert Settings().public_port == 0
    srv = live_factory(None)
    assert srv.public_base is None
    with srv.admin_client() as adm:
        adm.put("/admin/sharing", json={"enabled": True}, headers=ADMIN)
        r = adm.post("/admin/invites", json={"public": True}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["error"] == "public_endpoint_not_configured"
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENSEEK_PUBLIC_PORT": "8787"})                      # collides with the API port
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENSEEK_PUBLIC_PORT": "8789", "OPENSEEK_PUBLIC_HOST": "no-port.example.org"})
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENSEEK_PUBLIC_PORT": "8789", "OPENSEEK_PUBLIC_HOST": "a b:443"})
    s = Settings.from_env({"OPENSEEK_PUBLIC_PORT": "8789", "OPENSEEK_PUBLIC_HOST": "openseek.example.org:443"})
    assert (s.public_port, s.public_host) == (8789, "openseek.example.org:443")


# ------------------------------------------------------------------------------------------ routing
def test_classification_is_exact_and_denies_everything_else():
    from openseek.app import guest_route_allowed
    ok = lambda m, p: classify(m, p, guest_route_allowed)   # noqa: E731
    assert ok("GET", "/v1/health") == "health" and ok("POST", "/v1/pair/start") == "pair"
    assert ok("POST", "/v1/lookup") == "api" and ok("GET", f"/v1/c/{'A' * 32}/{ID}/thumbs.vtt") == "cap"
    for m, p in [("POST", "/v1/previews"), ("GET", f"/v1/previews/{ID}"), ("POST", f"/v1/previews/{ID}/priority"),
                 ("POST", f"/v1/previews/{ID}/telemetry"), ("POST", f"/v1/previews/{ID}/alt-source"),
                 ("GET", "/admin/status"), ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/"),
                 ("HEAD", "/v1/health"), ("OPTIONS", "/v1/lookup"), ("PUT", "/v1/lookup"), ("GET", "/v1/lookup"),
                 ("GET", "/v1/health/"), ("GET", "//v1/health"), ("GET", "/v1/health/../admin/status"),
                 ("GET", f"/v1/c/{'A' * 32}/{ID}/../../admin"), ("GET", f"/v1/c/{'A' * 32}/{ID}/other"),
                 ("POST", "/v1/pair/start/"), ("DELETE", "/v1/pair/start"), ("PATCH", "/v1/pair"),
                 ("GET", "/v1/pair/start"), ("POST", "/V1/lookup")]:
        assert ok(m, p) is None, (m, p)


@needs_fixture
def test_public_surface_owner_tokens_and_every_private_route(pub):
    srv, origin = pub
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        body = _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
        _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        pid = body["preview_id"]
        # a pairing window is open on the owner side: still not reachable from the public listener
        code = adm.post("/admin/pair", headers=ADMIN).json()["code"]
        inv = make_invite(adm, label="far TV", public=True)
        assert inv["address"] == srv.settings.public_host and f"h={srv.settings.public_host.replace(':', '%3A')}" in inv["join"]
        reqs, njobs = origin.file_requests, len(srv.core.jobs.jobs)

        with srv.public_client() as anon:
            h = anon.get("/v1/health")
            assert h.status_code == 200 and set(h.json()) == {"service", "api"}      # no version, ffmpeg or pairing state
            assert anon.post("/v1/pair/start", json={}).status_code == 409           # the owner's window is not public
            st = srv.client().post("/v1/pair/start", json={}).json()                 # (it IS open on the LAN listener)
            k = pairing.derive_key(code, st["pair_id"], st["salt"], st["spki_pin"], st["iterations"])
            r = anon.post("/v1/pair/claim", json={"pair_id": st["pair_id"], "device_name": "x",
                                                  "client_proof": pairing.client_proof(k, st["pair_id"], "x")})
            assert r.status_code == 403                                              # ... and cannot be claimed here either
        r = pjoin(srv, inv["invite_id"], inv["code"])
        assert r.status_code == 200 and r.json()["role"] == "guest"
        gtok = r.json()["token"]

        private = [("POST", "/v1/previews", {"source": {"url": _url(origin, "other_10min.mkv")}, "layout": "tiles"}),
                   ("GET", f"/v1/previews/{pid}", None), ("POST", f"/v1/previews/{pid}/priority", {}),
                   ("POST", f"/v1/previews/{pid}/telemetry", {}), ("POST", f"/v1/previews/{pid}/alt-source", {}),
                   ("GET", "/admin/status", None), ("PUT", "/admin/sharing", {"enabled": False}),
                   ("POST", "/admin/invites", {}), ("GET", "/docs", None), ("GET", "/openapi.json", None),
                   ("GET", "/admin/library", None), ("DELETE", "/admin/devices/" + "0" * 16, None)]
        for token, who in ((None, "anonymous"), (gtok, "guest"), (otok, "owner")):
            with srv.public_client(token) as c:
                for m, p, j in private:
                    r = c.request(m, p, json=j)
                    assert r.status_code in (404, 403) and "service" not in r.text, (who, m, p, r.status_code)
        # a VALID owner token is refused on every route, including the ones a guest may use
        with srv.public_client(otok) as c:
            for m, p, j in [("POST", "/v1/lookup", {"imdb_id": "tt1856101", "duration_ms": 600000}), ("GET", "/v1/pair/status", None),
                            ("GET", "/v1/health", None), ("DELETE", "/v1/pair", None)]:
                r = c.request(m, p, json=j)
                assert r.status_code == 403 and r.json()["error"] == "forbidden_role", (m, p)
        # an owner's own preview capability is never served on the public listener (but is on the LAN one)
        with srv.client(otok) as owner2:
            own = owner2.get(f"/v1/previews/{pid}").json()["vtt_url"]
        assert httpx.get(own, verify=False).status_code == 200
        pub_url = own.replace(srv.base, srv.public_base)
        assert httpx.get(pub_url, verify=False).status_code == 403
        with srv.public_client(gtok) as g:                                           # anonymous lookups are refused
            assert g.post("/v1/lookup", json={"imdb_id": "tt1856101", "duration_ms": 600000}).status_code in (200, 404)
        with srv.public_client() as anon:
            assert anon.post("/v1/lookup", json={"imdb_id": "tt1856101", "duration_ms": 600000}).status_code == 401
        # nothing above started a job or touched the CDN
        assert origin.file_requests == reqs and len(srv.core.jobs.jobs) == njobs


# ------------------------------------------------------------------------------------------ limits
def test_limits_use_the_peer_address_and_ignore_forwarding_headers(live_factory):
    port = free_port()
    srv = live_factory(None, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
    srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={"health": 5, "pair": 3, "api": 5, "cap": 5, "unknown": 5},
                                                         strikes=4, block_s=900))
    spoof = [{"X-Forwarded-For": f"203.0.113.{i}", "X-Real-IP": f"198.51.100.{i}", "Forwarded": f"for=192.0.2.{i}"} for i in range(40)]
    with srv.public_client() as c:
        codes = [c.get("/v1/health", headers=spoof[i]).status_code for i in range(7)]
        assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]                    # rotating the claimed origin evades nothing
    with srv.public_client() as c:                                                   # strikes: unknown routes, bad pairing
        srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={"health": 50, "pair": 50, "api": 50, "cap": 50, "unknown": 50},
                                                             strikes=4, block_s=900))
        for i in range(4):
            assert c.get(f"/wp-admin/{i}", headers=spoof[i]).status_code == 404
        r = c.get("/v1/health", headers=spoof[9])
        assert r.status_code == 429 and r.headers["retry-after"] == "900"


def test_strikes_expire_and_blocks_lift_with_time():
    now = [0.0]
    lim = PublicLimiter(PublicLimits(per_minute={"api": 3}, strikes=3, strike_window_s=100, block_s=500), clock=lambda: now[0])
    for _ in range(3):
        lim.strike("1.2.3.4")
    assert lim.blocked("1.2.3.4") and not lim.blocked("5.6.7.8")
    now[0] = 501
    assert not lim.blocked("1.2.3.4")
    lim.strike("9.9.9.9"); lim.strike("9.9.9.9"); now[0] = 700; lim.strike("9.9.9.9")        # window slid: only one live strike
    assert not lim.blocked("9.9.9.9")
    assert [lim.allow("a", "api") for _ in range(4)] == [True, True, True, False]
    now[0] = 800
    assert lim.allow("a", "api")


def test_wrong_proofs_burn_an_invite_and_strike_the_sender(live_factory):
    port = free_port()
    srv = live_factory(None, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
    with srv.admin_client() as adm:
        adm.put("/admin/sharing", json={"enabled": True}, headers=ADMIN)
        inv = make_invite(adm, public=True)
        wrong = inv["code"][:-1] + ("2" if inv["code"][-1] != "2" else "3")
        seen = [pjoin(srv, inv["invite_id"], inv["code"], proof_code=wrong[-12:].replace("-", "")).status_code for _ in range(5)]
        assert set(seen) <= {403, 429}
        assert pjoin(srv, inv["invite_id"], inv["code"]).status_code in (403, 409, 429)   # burned / blocked: even the right code fails


# ------------------------------------------------------------------------------------------ capabilities
@needs_fixture
def test_borrowed_urls_follow_guest_identity_revocation_the_kill_switch_and_never_echo_the_host_header(pub):
    srv, origin = pub
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        body = _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
        _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        cache_ms = srv.core.cache.entry(next(iter(srv.core.jobs.sources.values())).fp).duration_ms
        reqs, njobs = origin.file_requests, len(srv.core.jobs.jobs)
        ia, ib = make_invite(adm, public=True, label="A"), make_invite(adm, public=True, label="B")
        spare = make_invite(adm, public=True, label="spare")
        a, b = pjoin(srv, ia["invite_id"], ia["code"], "a").json(), pjoin(srv, ib["invite_id"], ib["code"], "b").json()
        want = {"imdb_id": "tt1856101", "duration_ms": cache_ms + 7000, "layout": "tiles"}
        with srv.public_client(a["token"]) as ga, srv.public_client(b["token"]) as gb:
            ca = ga.post("/v1/lookup", json=want, headers={"Host": "evil.example.net"}).json()["candidates"][0]
            cb = gb.post("/v1/lookup", json=want).json()["candidates"][0]
            assert ca["vtt_url"].startswith(f"https://{srv.settings.public_host}/v1/c/")       # configured name, not the Host header
            assert ca["preview_id"] != cb["preview_id"]
            vtt = httpx.get(ca["vtt_url"], verify=False, headers={"Host": "evil.example.net"})
            assert vtt.status_code == 200 and "evil.example.net" not in vtt.text and srv.settings.public_host in vtt.text
            tile = next(l for l in vtt.text.splitlines() if l.startswith("https://")).split("#")[0]
            assert httpx.get(tile, verify=False).content[:2] == b"\xff\xd8"
            # capability for A's preview: unusable with the wrong preview id / tampered cap / expired exp
            assert httpx.get(ca["vtt_url"].replace(ca["preview_id"], cb["preview_id"]), verify=False).status_code == 403
            cap = ca["vtt_url"].split("/v1/c/")[1].split("/")[0]
            assert httpx.get(ca["vtt_url"].replace(cap, "A" * len(cap)), verify=False).status_code == 403
            assert httpx.get(ca["vtt_url"].replace("exp=", "exp=1"), verify=False).status_code == 403
            # kill switch: guests and their URLs stop on the PUBLIC listener at once; the owner is unaffected
            adm.put("/admin/sharing", json={"enabled": False}, headers=ADMIN)
            assert ga.post("/v1/lookup", json=want).status_code == 403
            assert httpx.get(ca["vtt_url"], verify=False).status_code == 403
            assert pjoin(srv, spare["invite_id"], spare["code"]).status_code == 409          # no new guest while sharing is off
            assert owner.get(f"/v1/previews/{body['preview_id']}").status_code == 200
            adm.put("/admin/sharing", json={"enabled": True}, headers=ADMIN)
            assert httpx.get(ca["vtt_url"], verify=False).status_code == 200
            # revocation: B is gone immediately, A is not affected
            assert adm.delete(f"/admin/devices/{b['device_id']}", headers=ADMIN).status_code == 204
            assert gb.post("/v1/lookup", json=want).status_code == 401
            assert httpx.get(cb["vtt_url"], verify=False).status_code == 403
            assert httpx.get(ca["vtt_url"], verify=False).status_code == 200
        assert origin.file_requests == reqs and len(srv.core.jobs.jobs) == njobs


# ------------------------------------------------------------------------------------------ restart / recovery
def test_a_restart_keeps_guests_and_invitations_and_leaves_the_listener_off_unless_configured(live_factory, tmp_path):
    port = free_port()
    d = tmp_path / "restart"
    srv = live_factory(None, data_dir=d, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
    with srv.admin_client() as adm:
        adm.put("/admin/sharing", json={"enabled": True}, headers=ADMIN)
        inv = make_invite(adm, public=True)
        tok = pjoin(srv, inv["invite_id"], inv["code"]).json()["token"]
        spare = make_invite(adm, public=True)
    srv.stop()
    port2 = free_port()
    srv2 = live_factory(None, data_dir=d, public_port=port2, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port2}")
    with srv2.public_client(tok) as g:
        assert g.get("/v1/pair/status").status_code == 200                           # the same guest, after a restart
    assert pjoin(srv2, spare["invite_id"], spare["code"]).status_code == 200         # the unused invitation survived
    srv2.stop()
    srv3 = live_factory(None, data_dir=d)                                             # rollback = start without OPENSEEK_PUBLIC_PORT
    assert srv3.public_base is None
    with pytest.raises(httpx.TransportError):
        httpx.get(f"https://127.0.0.1:{port2}/v1/health", verify=False, timeout=3)  # nothing is listening there any more


def test_the_external_probe_passes_against_a_correctly_configured_listener_and_fails_against_a_wrong_pin(live_factory):
    import subprocess
    import sys
    from pathlib import Path
    port = free_port()
    srv = live_factory(None, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
    with srv.admin_client() as adm:
        sharing_on(adm)
        inv = make_invite(adm, public=True)
    tok = pjoin(srv, inv["invite_id"], inv["code"]).json()["token"]
    probe = Path(__file__).parent.parent / "tools" / "public_probe.py"
    cmd = [sys.executable, str(probe), f"127.0.0.1:{port}", "--pin", srv.core.spki_pin, "--token", tok, "--closed-ports", "1"]
    ok = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0 and "FAIL" not in ok.stdout, ok.stdout + ok.stderr
    assert tok not in ok.stdout + ok.stderr                                          # the token is never printed
    cmd[cmd.index("--pin") + 1] = "sha256//" + "A" * 43 + "="
    srv.admin_client().delete("/admin/public/blocks", headers=ADMIN)
    bad = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert bad.returncode == 1 and "[FAIL] certificate pin matches" in bad.stdout
