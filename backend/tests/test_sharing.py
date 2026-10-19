"""Stage 2: owner/guest roles, one-time invitations on OSP1, read-only guest access, kill switch, revocation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from helpers import wait_for
from origin import Origin
from openseek import pairing
from openseek.app import GUEST_ALLOWED, create_api_app, guest_route_allowed
from test_integration import _create, _policy, _status, _url
from test_scenarios import _register

from helpers import FIXTURES as ALT, FIXTURES_HINT
needs_fixture = pytest.mark.skipif(not (ALT / "ok_10min.mkv").exists(), reason=FIXTURES_HINT)
ADMIN = {"X-OpenSeek-Admin": "test-admin-token"}
HINTS = {"imdb_id": "tt1856101"}
FAST_ITER = 200_000                       # the client minimum; keeps the key derivation quick in tests


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    monkeypatch.setattr(pairing, "ITERATIONS", FAST_ITER)


def anon(srv):
    return httpx.Client(base_url=srv.base, verify=False, timeout=30)


def join(srv, invite_id: str, code: str, name: str = "her-tv", proof_code: str | None = None) -> httpx.Response:
    """A device joining through an invitation: exactly the OSP1 steps a client performs. `code` is what a person
    types (tag + secret); the invitation is named by its id (join string) or, when only a typed code exists, its tag."""
    plain = code.replace("-", "")
    secret = plain[4:] if len(plain) == 16 else plain
    code = secret
    with anon(srv) as c:
        s = c.post("/v1/pair/start", json={"invite": invite_id})
        if s.status_code != 200:
            return s
        s = s.json()
        k = pairing.derive_key(proof_code or code, s["pair_id"], s["salt"], s["spki_pin"], s["iterations"])
        r = c.post("/v1/pair/claim", json={"pair_id": s["pair_id"], "device_name": name,
                                           "client_proof": pairing.client_proof(k, s["pair_id"], name)})
        if r.status_code == 200:
            body = r.json()
            assert body["server_proof"] == pairing.server_proof(k, s["pair_id"], body["token"])    # mutual authentication
        return r


def make_invite(adm, **body):
    r = adm.post("/admin/invites", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def sharing_on(adm, library=True):
    assert adm.put("/admin/sharing", json={"enabled": True}, headers=ADMIN).json()["enabled"] is True
    if library:
        adm.put("/admin/library", json={"metadata_index": True}, headers=ADMIN)


@pytest.fixture()
def rig(live_factory):
    o = Origin(ALT).start() if ALT.exists() else None
    srv = live_factory(_policy(o) if o else None, pregen="full", workers_per_source=2, create_wait_s=10.0) if o else None
    yield srv, o
    if o:
        o.stop()


# ------------------------------------------------------------------------------------------ invitations
def test_sharing_is_off_by_default_and_no_invite_can_be_made_or_used_while_off(live_factory):
    srv = live_factory(None)
    with srv.admin_client() as adm:
        assert adm.get("/admin/sharing").json()["enabled"] is False
        assert adm.post("/admin/invites", json={}, headers=ADMIN).status_code == 409
        sharing_on(adm, library=False)
        inv = make_invite(adm)
        adm.put("/admin/sharing", json={"enabled": False}, headers=ADMIN)
        assert join(srv, inv["invite_id"], inv["code"]).status_code in (403, 409)       # the switch is off: nobody joins


def test_invite_is_one_time_strong_and_the_code_is_never_stored(live_factory):
    srv = live_factory(None)
    with srv.admin_client() as adm:
        sharing_on(adm, library=False)
        inv = make_invite(adm, label="her TV")
        assert len(inv["code"].replace("-", "")) == 16 and len(inv["secret"].replace("-", "")) == 12 and inv["role"] == "guest"
        assert inv["code"].replace("-", "") == inv["tag"] + inv["secret"].replace("-", "")
        assert inv["join"].endswith("&c=" + inv["secret"])
        by_tag = join(srv, inv["tag"], inv["code"], name="typed-code-device")             # a typed code names its own invitation
        assert by_tag.status_code == 200 and by_tag.json()["role"] == "guest"
        inv = make_invite(adm, label="her TV")
        assert inv["join"].startswith("openseek://join/1?h=") and inv["invite_id"] in inv["join"]
        raw = Path(srv.core.db.path).read_bytes()
        for p in Path(srv.core.db.path).parent.glob("openseek.db-wal"):
            raw += p.read_bytes()
        assert inv["code"].replace("-", "").encode() not in raw and inv["code"].encode() not in raw
        ok = join(srv, inv["invite_id"], inv["code"])
        assert ok.status_code == 200 and ok.json()["role"] == "guest" and len(ok.json()["token"]) == 43
        again = join(srv, inv["invite_id"], inv["code"], name="second-try")
        assert again.status_code in (403, 409)                                          # one use: a replay or a second device fails
        row = adm.get("/admin/sharing").json()
        assert [i["state"] for i in row["invites"]] == ["used", "used"] and len(row["guests"]) == 2


def test_wrong_codes_burn_the_invite_after_five_attempts_even_across_restarts_of_the_counter(live_factory):
    srv = live_factory(None)
    with srv.admin_client() as adm:
        sharing_on(adm, library=False)
        inv = make_invite(adm)
        for _ in range(5):
            assert join(srv, inv["invite_id"], inv["code"], proof_code="AAAA-AAAA-AAAA").status_code == 403
            time.sleep(0.2)
        assert adm.get("/admin/invites").json()["invites"][0]["state"] == "burned"
        assert join(srv, inv["invite_id"], inv["code"]).status_code in (403, 409)       # even the right code is refused now


def test_expired_and_revoked_invites_and_malformed_requests_are_refused_alike(live_factory):
    srv = live_factory(None)
    with srv.admin_client() as adm:
        sharing_on(adm, library=False)
        expired, revoked, good = make_invite(adm), make_invite(adm), make_invite(adm)
        srv.core.invites.clock = lambda: time.time() + 8 * 86400                        # past the 7-day maximum
        assert join(srv, expired["invite_id"], expired["code"]).status_code in (403, 409)
        assert adm.get("/admin/invites").json()["invites"][0]["state"] == "expired"
        srv.core.invites.clock = time.time
        assert adm.delete(f"/admin/invites/{revoked['invite_id']}", headers=ADMIN).status_code == 204
        assert join(srv, revoked["invite_id"], revoked["code"]).status_code in (403, 409)
        assert adm.delete(f"/admin/invites/{'0' * 32}", headers=ADMIN).status_code == 404
        with anon(srv) as c:
            for body in ({"invite": "x" * 5}, {"invite": "../../etc"}, {"invite": "0" * 32}, {"invite": 5}):
                assert c.post("/v1/pair/start", json=body).status_code in (400, 409, 422)
            assert c.post("/v1/pair/claim", json={"pair_id": good["invite_id"], "device_name": "x", "client_proof": "zz"}).status_code in (400, 403)
            assert c.post("/v1/pair/claim", json={"pair_id": good["invite_id"], "device_name": "\x00bad", "client_proof": "0" * 64}).status_code in (400, 403)
        assert join(srv, good["invite_id"], good["code"]).status_code == 200            # a good invite is unaffected by all that


def test_an_invite_cannot_mint_an_owner_and_the_claim_cannot_choose_its_role(live_factory):
    srv = live_factory(None)
    with srv.admin_client() as adm:
        sharing_on(adm, library=False)
        inv = make_invite(adm)
        with anon(srv) as c:
            s = c.post("/v1/pair/start", json={"invite": inv["invite_id"]}).json()
            k = pairing.derive_key(inv["secret"], s["pair_id"], s["salt"], s["spki_pin"], s["iterations"])
            r = c.post("/v1/pair/claim", json={"pair_id": s["pair_id"], "device_name": "sneaky", "role": "owner", "extra": 1,
                                               "client_proof": pairing.client_proof(k, s["pair_id"], "sneaky")})
        assert r.status_code == 200 and r.json()["role"] == "guest"
        assert srv.core.auth.role_of(r.json()["device_id"]) == "guest"


def test_device_access_expiry_is_honoured(live_factory):
    srv = live_factory(None)
    with srv.admin_client() as adm:
        sharing_on(adm, library=False)
        inv = make_invite(adm, device_days=1)
        tok = join(srv, inv["invite_id"], inv["code"]).json()["token"]
        with srv.client(tok) as g:
            assert g.get("/v1/pair/status").status_code == 200
            srv.core.db.x("UPDATE devices SET expires=? WHERE role='guest'", (time.time() - 5,))
            srv.core.auth.reload()
            assert g.get("/v1/pair/status").status_code == 401


# ------------------------------------------------------------------------------------------ route permissions
def test_every_route_a_guest_is_not_explicitly_allowed_is_refused(live_factory):
    srv = live_factory(None)
    with srv.admin_client() as adm:
        sharing_on(adm, library=False)
        inv = make_invite(adm)
        tok = join(srv, inv["invite_id"], inv["code"]).json()["token"]
    pid = "0" * 32
    sample = {"preview_id": pid, "cap": "A" * 32, "name": "0.jpg"}
    with srv.client(tok) as g:
        seen = 0
        for route in create_api_app(srv.core).app.routes:
            path = getattr(route, "path", None)
            if not path or not path.startswith("/v1/") or path.startswith("/v1/c/"):
                continue
            concrete = path
            for k, v in sample.items():
                concrete = concrete.replace("{" + k + "}", v)
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                if guest_route_allowed(method, concrete) or concrete in ("/v1/pair/start", "/v1/pair/claim"):
                    continue                                                          # allowed (or public pairing steps)
                r = g.request(method, concrete, json={})
                assert r.status_code == 403 and r.json()["error"] == "forbidden_role", (method, path, r.status_code, r.text)
                seen += 1
        assert seen >= 5                                                               # previews, priority, alt-source, telemetry, ...
        for method, path in (("POST", "/v1/previews"), ("POST", f"/v1/previews/{pid}/priority"), ("POST", f"/v1/previews/{pid}/telemetry"),
                             ("POST", f"/v1/previews/{pid}/alt-source"), ("GET", f"/v1/previews/{pid}"), ("GET", "/admin/status"),
                             ("DELETE", "/admin/devices/" + pid), ("POST", "/admin/invites")):
            r = g.request(method, path, json={"source": {"url": "http://example.com/x.mkv"}})
            assert r.status_code in (403, 404), (method, path, r.status_code)         # never 2xx
    assert {m for m, _ in GUEST_ALLOWED} == {"GET", "PUT", "POST", "DELETE"}


# ------------------------------------------------------------------------------------------ the acceptance demonstration
@needs_fixture
def test_owner_shares_guest_pairs_reads_a_cached_timeline_cannot_extract_and_revocation_is_immediate(rig):
    srv, origin = rig
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        # 1. the owner opts in (sharing + title index) and has one title cached
        sharing_on(adm)
        body = _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
        _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)       # records the ids (opt-in is on)
        src = next(iter(srv.core.jobs.sources.values()))
        cache_ms = srv.core.cache.entry(src.fp).duration_ms
        owner_preview = body["preview_id"]
        reqs, nsrc, njobs = origin.file_requests, len(srv.core.jobs.sources), len(srv.core.jobs.jobs)

        # 2. invitation -> 3. a second device pairs with it
        inv = make_invite(adm, label="her TV")
        r = join(srv, inv["invite_id"], inv["code"])
        assert r.status_code == 200 and r.json()["role"] == "guest"
        gtok, gid = r.json()["token"], r.json()["device_id"]
        with srv.client(gtok) as guest:
            assert guest.get("/v1/pair/status").json()["device_id"] == gid

            # 4. a different release of the cached title (7 s longer): found, flagged approximate
            want = {"imdb_id": "tt1856101", "duration_ms": cache_ms + 7000, "layout": "tiles"}
            found = guest.post("/v1/lookup", json=want)
            assert found.status_code == 200, found.text
            cand = found.json()["candidates"][0]
            assert cand["approximate"] and not cand["verified"] and cand["source"]["coverage"] == 1.0

            # 5. the existing VTT and JPEGs, through the capability URL (no bearer needed, bound to this device)
            with anon(srv) as plain:
                vtt = plain.get(cand["vtt_url"])
                assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")
                tile_url = next(l for l in vtt.text.splitlines() if l.startswith("https://")).split("#")[0]
                jpg = plain.get(tile_url)
                assert jpg.status_code == 200 and jpg.content[:2] == b"\xff\xd8"
                # the owner's own preview id is not readable through a guest capability, nor with a guest bearer
                assert guest.get(f"/v1/c/{'A' * 32}/{owner_preview}/thumbs.vtt?exp=1").status_code == 403
                assert guest.get(f"/v1/previews/{owner_preview}").status_code == 403

            # 6. no extraction, no URL submission, no controls, no admin
            r = guest.post("/v1/previews", json={"source": {"url": _url(origin, "other_10min.mkv")}, "layout": "tiles"})
            assert r.status_code == 403 and r.json()["error"] == "forbidden_role"
            for path in (f"/v1/previews/{owner_preview}/priority", f"/v1/previews/{owner_preview}/telemetry",
                         f"/v1/previews/{owner_preview}/alt-source"):
                assert guest.post(path, json={}).status_code == 403
            assert guest.delete(f"/v1/previews/{owner_preview}").status_code == 404          # allowed route, but it is not theirs
            assert guest.get("/admin/status").status_code in (403, 404)
            assert origin.file_requests == reqs and len(srv.core.jobs.sources) == nsrc and len(srv.core.jobs.jobs) == njobs
            assert owner.get(f"/v1/previews/{owner_preview}").status_code == 200       # the owner's own preview was never disturbed

            # kill switch: every guest call and the guest's capability URLs stop; the owner is unaffected
            adm.put("/admin/sharing", json={"enabled": False}, headers=ADMIN)
            assert guest.post("/v1/lookup", json=want).status_code == 403
            assert guest.post("/v1/lookup", json=want).json()["error"] == "sharing_disabled"
            with anon(srv) as plain:
                assert plain.get(cand["vtt_url"]).status_code == 403 and plain.get(tile_url).status_code == 403
            assert owner.get(f"/v1/previews/{owner_preview}").status_code == 200
            adm.put("/admin/sharing", json={"enabled": True}, headers=ADMIN)
            assert guest.post("/v1/lookup", json=want).status_code == 200

            # 7. the owner revokes the guest: the very next request fails, and so does the URL it already held
            assert adm.delete(f"/admin/devices/{gid}", headers=ADMIN).status_code == 204
            assert guest.get("/v1/pair/status").status_code == 401
            assert guest.post("/v1/lookup", json=want).status_code == 401
            with anon(srv) as plain:
                assert plain.get(cand["vtt_url"]).status_code == 403 and plain.get(tile_url).status_code == 403
            assert srv.core.db.q("SELECT COUNT(*) FROM borrowed WHERE device_id=?", (gid,))[0][0] == 0


@needs_fixture
def test_guests_are_isolated_from_each_other_offsets_rejections_and_previews(rig):
    srv, origin = rig
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        body = _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
        _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        cache_ms = srv.core.cache.entry(next(iter(srv.core.jobs.sources.values())).fp).duration_ms
        toks = []
        for n in ("her-tv", "his-phone"):
            inv = make_invite(adm, label=n)
            toks.append(join(srv, inv["invite_id"], inv["code"], name=n).json())
        want = {"imdb_id": "tt1856101", "duration_ms": cache_ms + 7000, "layout": "tiles"}
        with srv.client(toks[0]["token"]) as g1, srv.client(toks[1]["token"]) as g2:
            c1 = g1.post("/v1/lookup", json=want).json()["candidates"][0]
            c2 = g2.post("/v1/lookup", json=want).json()["candidates"][0]
            assert c1["preview_id"] != c2["preview_id"]
            assert g1.put(f"/v1/previews/{c1['preview_id']}/offset", json={"offset_ms": -7000}).status_code == 200
            assert g2.put(f"/v1/previews/{c1['preview_id']}/offset", json={"offset_ms": 1}).status_code == 404
            assert g2.post(f"/v1/previews/{c1['preview_id']}/reject").status_code == 404
            assert g2.post("/v1/lookup", json=want).json()["candidates"][0]["mapping"]["b"] == 0     # unaffected by g1's offset
            assert g1.post(f"/v1/previews/{c1['preview_id']}/reject").status_code == 204
            assert g1.post("/v1/lookup", json=want).status_code == 404                                # hidden for g1 only
            assert g2.post("/v1/lookup", json=want).status_code == 200
            with anon(srv) as plain:                                                                   # g2 cannot use g1's URL
                assert plain.get(c2["vtt_url"]).status_code == 200


# ------------------------------------------------------------------------------------------ migration
def test_an_existing_database_gains_roles_without_losing_devices(tmp_path):
    from openseek.db import DB
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE devices (device_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, "
                      "created REAL NOT NULL, last_seen REAL NOT NULL); INSERT INTO devices VALUES('d1','tv','h',1,1);")
    con.commit()
    con.close()
    db = DB(p)
    assert db.devices()[0]["role"] == "owner" and db.auth_rows()[0][2] == "owner"            # existing devices stay owners
    db.close()
    db = DB(p)                                                                                # migrating twice is harmless
    assert len(db.devices()) == 1
    db.close()
