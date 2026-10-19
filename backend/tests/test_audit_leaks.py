"""Audit probes on an isolated server: what a guest or anonymous client can LEARN (not just do)."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from helpers import free_port, wait_for
from origin import Origin
from test_integration import _create, _policy, _status, _url
from test_public_listener import pjoin
from test_scenarios import _register
from test_sharing import ALT, ADMIN, HINTS, make_invite, needs_fixture, sharing_on


@needs_fixture
def test_what_a_guest_and_an_anonymous_client_can_learn(live_factory):
    o = Origin(ALT).start()
    try:
        port = free_port()
        srv = live_factory(_policy(o), pregen="full", workers_per_source=2, create_wait_s=10.0, public_port=port,
                           public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
        from openseek.public import PublicLimiter, PublicLimits
        srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")}, strikes=10 ** 6))
        otok, _ = _register(srv, "owner-tv")
        with srv.client(otok) as owner, srv.admin_client() as adm:
            sharing_on(adm)
            b = _create(owner, _url(o, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
            wait_for(lambda: _status(owner, b["preview_id"])["state"] == "complete", 120, 0.1)
            _create(owner, _url(o, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
            cache_ms = srv.core.cache.entry(next(iter(srv.core.jobs.sources.values())).fp).duration_ms
            inv = make_invite(adm, public=True)
            tok = pjoin(srv, inv["invite_id"], inv["code"]).json()["token"]
            out = {}
            with srv.public_client(tok) as g:
                hit = g.post("/v1/lookup", json={"imdb_id": HINTS["imdb_id"], "duration_ms": cache_ms, "layout": "tiles"})
                out["lookup 200 keys"] = sorted(hit.json()["candidates"][0].keys())
                out["lookup candidate.source keys"] = sorted(hit.json()["candidates"][0]["source"].keys())
                miss = g.post("/v1/lookup", json={"imdb_id": "tt0000001", "duration_ms": cache_ms, "layout": "tiles"})
                out["guest can tell cached vs not cached (200 vs 404)"] = (hit.status_code, miss.status_code)
                out["guest pair/status keys"] = sorted(g.get("/v1/pair/status").json().keys())
            with srv.public_client() as anon:
                known = anon.post("/v1/pair/start", json={"invite": inv["invite_id"]})
                unknown = anon.post("/v1/pair/start", json={"invite": "0" * 32})
                tag_known = make_invite(adm, public=True)
                t1 = anon.post("/v1/pair/start", json={"invite": tag_known["tag"]})
                t2 = anon.post("/v1/pair/start", json={"invite": "ZZZZ"})
                out["invite oracle: used-id / random-id / live-tag / random-tag"] = (known.status_code, unknown.status_code, t1.status_code, t2.status_code)
                out["unknown invite body"] = unknown.json()
            print("\nLEAK PROBE " + json.dumps(out, indent=1, default=str))
    finally:
        o.stop()
