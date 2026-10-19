"""Public listener: encoded-path / method bypass attempts, lock-out abuse, kill switch vs a running extraction."""
from __future__ import annotations

import http.client
import ssl

import httpx
import pytest

from helpers import wait_for
from openseek.public import PublicLimiter
from test_integration import _create, _status, _url
from test_public_listener import pjoin, pub  # noqa: F401  (pub is a fixture)
from test_scenarios import _register
from test_sharing import ADMIN, HINTS, make_invite, needs_fixture, sharing_on


def raw(port: int, method: str, target: str, headers: dict | None = None, body: bytes = b"", token: str | None = None):
    """A request line sent EXACTLY as given (no client-side normalisation of %xx or dot segments)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    try:
        c.putrequest(method, target, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        return r.status, r.read(2048)
    finally:
        c.close()


@needs_fixture
def test_encoded_paths_alternative_methods_and_override_headers_never_reach_private_routes(pub):
    srv, origin = pub
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        body = _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
        pid = body["preview_id"]
        inv = make_invite(adm, public=True)
        gtok = pjoin(srv, inv["invite_id"], inv["code"]).json()["token"]
        reqs, njobs = origin.file_requests, len(srv.core.jobs.jobs)
        port = srv.settings.public_port
        bs = chr(92)
        targets = ["/v1/%70reviews", "/v1/previews%2f", f"/v1/previews/{pid}", f"/v1/%70reviews/{pid}/priority",
                   f"/v1/previews/{pid}/telemetry", f"/v1/previews/{pid}%2fpriority", f"/v1/previews/{pid}/alt-source",
                   "/admin/status", "/%61dmin/status", "/v1/health/..%2fadmin%2fstatus", "/v1/health/../admin/status",
                   "/v1/health%2f..%2f..%2fadmin", "/%2e%2e/admin/status", "/v1//previews", "//admin/status",
                   "/v1" + bs + "previews", "/v1/previews%00", "/v1/lookup%00", "/v1/lookup/", "/V1/LOOKUP",
                   "/v1/pair%2fstart", "/v1/pair/start%20", f"/v1/c/{'A' * 32}/{pid}/%2e%2e/%2e%2e/admin/status",
                   f"/v1/c/{'A' * 32}/{pid}/thumbs.vtt%2f..%2f..", "/docs", "/openapi.json", "/redoc", "/",
                   "/static/../../etc/passwd", "/v1/previews?x=/v1/health", "/v1/health#/admin"]
        methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]
        hdr = {"X-HTTP-Method-Override": "DELETE", "X-Original-URL": "/admin/status", "X-Rewrite-URL": "/admin/status"}
        answered = 0
        for token in (None, gtok, otok):
            for t in targets:
                for m in methods:
                    st, b = raw(port, m, t, hdr, b"{}", token)
                    answered += 1
                    assert st in (400, 401, 403, 404, 405, 409, 429, 501), (m, t, st, b[:80])
                    # the only 200s on this surface are the three open GETs; none of these targets is one of them,
                    # except "/v1/health#/admin" (the fragment is never sent) and "/v1/health" aliases
                    if st == 200:
                        assert t in ("/v1/health#/admin",), (m, t, token and "token")
        assert answered == 3 * len(targets) * len(methods)
        with srv.public_client(gtok) as g:
            assert g.get("/v1/pair/status").status_code == 200
        assert origin.file_requests == reqs and len(srv.core.jobs.jobs) == njobs
        assert owner.get(f"/v1/previews/{pid}").status_code == 200              # the owner's LAN API is untouched


@needs_fixture
def test_a_hostile_address_cannot_lock_out_legitimate_guests_or_the_owner(pub):
    srv, origin = pub
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        inv = make_invite(adm, public=True)
        srv.core.public_limiter = PublicLimiter()                  # the REAL limits, not the permissive fixture ones
        gtok = pjoin(srv, inv["invite_id"], inv["code"]).json()["token"]
        with srv.public_client(gtok) as g:
            for _ in range(40):                                    # "no match" answers are normal and never a strike
                assert g.post("/v1/lookup", json={"imdb_id": "tt0000001", "duration_ms": 600000}).status_code == 404
            assert srv.core.public_limiter.snapshot()["blocked"] == 0

        def from_addr(ip: str) -> httpx.Client:
            return httpx.Client(base_url=srv.public_base, transport=httpx.HTTPTransport(local_address=ip, verify=False), timeout=10)

        with from_addr("127.0.0.2") as evil:                       # a hostile address racks up refusals and is blocked
            codes = [evil.get(f"/wp-login/{i}").status_code for i in range(25)]
            assert codes[:20] == [404] * 20 and set(codes[20:]) == {429}
            assert evil.get("/v1/health").status_code == 429
        with srv.public_client(gtok) as g:                         # the guest (other address), the owner and the admin are unaffected
            assert g.get("/v1/pair/status").status_code == 200
        assert owner.get("/v1/health").status_code == 200
        assert srv.core.public_limiter.snapshot()["blocked"] == 1
        assert adm.delete("/admin/public/blocks", headers=ADMIN).status_code == 204       # the owner lifts it at once
        with from_addr("127.0.0.2") as evil:
            assert evil.get("/v1/health").status_code == 200
        with from_addr("127.0.0.3") as flood:                      # a pairing flood stops at the per-address budget
            seen = [flood.post("/v1/pair/start", json={"invite": "0" * 32}).status_code for _ in range(14)]
            assert seen.count(429) >= 1
        ok = make_invite(adm, public=True)                         # and legitimate pairing from the original address still works
        assert pjoin(srv, ok["invite_id"], ok["code"], "second").status_code == 200


@needs_fixture
def test_turning_sharing_off_does_not_disturb_an_owner_extraction_in_progress(pub):
    srv, origin = pub
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        inv = make_invite(adm, public=True)
        gtok = pjoin(srv, inv["invite_id"], inv["code"]).json()["token"]
        body = _create(owner, _url(origin, "other_10min.mkv"), at_ms=0, layout="tiles", hints={"imdb_id": "tt0000002"})
        adm.put("/admin/sharing", json={"enabled": False}, headers=ADMIN)               # instant, mid-extraction
        with srv.public_client(gtok) as g:
            assert g.post("/v1/lookup", json={"imdb_id": "tt0000002", "duration_ms": 600000}).status_code == 403
        wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
        assert owner.get(f"/v1/previews/{body['preview_id']}").json()["state"] == "complete"
