"""Guest contributions: Reader vs Contributor, narrow API, SSRF policy, limits, owner protection, revocation, kill switch."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from helpers import free_port, wait_for
from origin import Origin
from openseek.contrib import ContributionPolicy, clean_hosts
from openseek.jobs import JobManager
from openseek.netpolicy import Denied, DestinationPolicy
from openseek.public import PublicLimiter, PublicLimits
from test_integration import HOST, _create, _policy, _status, _url
from test_public_listener import pjoin
from test_scenarios import _register
from test_sharing import ALT, ADMIN, make_invite, needs_fixture, sharing_on

IDS = {"imdb_id": "tt7777777", "season": 0, "episode": 0}


def contributor_body(url: str, **over):
    b = {"source": {"url": url}, "imdb_id": "tt7777777", "season": 0, "episode": 0, "duration_ms": 600_000}
    b.update(over)
    return b


def guest(srv, adm, label="g", read_only=False):
    inv = make_invite(adm, public=True, label=label, read_only=read_only)
    r = pjoin(srv, inv["invite_id"], inv["code"], label).json()
    return r["token"], r["device_id"]


@pytest.fixture()
def rig(live_factory):
    o = Origin(ALT).start() if ALT.exists() else None
    port = free_port()
    srv = live_factory(_policy(o) if o else None, pregen="full", workers_per_source=2, create_wait_s=10.0, public_port=port,
                       public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}", contrib_https_only=False)
    srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")}, strikes=10 ** 6))
    srv.core.contrib_spacing.n = 1000
    yield srv, o
    if o:
        o.stop()


def enable(adm, device_id=None, hosts=(HOST,)):
    assert adm.put("/admin/contributions", json={"enabled": True, "hosts": list(hosts)}, headers=ADMIN).status_code == 200
    if device_id:
        assert adm.put(f"/admin/devices/{device_id}/permissions", json={"contribute": True}, headers=ADMIN).status_code == 200


# --------------------------------------------------------------------------------------------- permissions
@needs_fixture
def test_reader_cannot_contribute_the_switch_defaults_off_and_each_step_is_needed(rig):
    srv, o = rig
    with srv.admin_client() as adm:
        sharing_on(adm)
        assert adm.get("/admin/contributions").json()["enabled"] is False                     # off by default
        tok, did = guest(srv, adm, read_only=True)           # an explicit Reader
        with srv.public_client(tok) as g:
            url = _url(o, "ok_10min.mkv")
            r = g.post("/v1/contribute", json=contributor_body(url))
            assert r.status_code == 403 and r.json()["error"] == "contribution_not_allowed"
            assert g.get("/v1/pair/status").json()["contribute"] is False
            adm.put("/admin/contributions", json={"enabled": True, "hosts": [HOST]}, headers=ADMIN)   # switch on, device still a Reader
            assert g.post("/v1/contribute", json=contributor_body(url)).status_code == 403
            adm.put(f"/admin/devices/{did}/permissions", json={"contribute": True}, headers=ADMIN)
            assert g.get("/v1/pair/status").json()["contribute"] is True
        # an owner device can never be made a Contributor, and unknown ids are refused
        otok, odev = _register(srv, "owner-tv")
        assert adm.put(f"/admin/devices/{odev}/permissions", json={"contribute": True}, headers=ADMIN).status_code == 404
        assert adm.put("/admin/devices/" + "0" * 16 + "/permissions", json={"contribute": True}, headers=ADMIN).status_code == 404
        # sharing off: even a permitted Contributor is refused
        adm.put("/admin/sharing", json={"enabled": False}, headers=ADMIN)
        with srv.public_client(tok) as g:
            assert g.post("/v1/contribute", json=contributor_body(_url(o, "ok_10min.mkv"))).status_code == 403


# --------------------------------------------------------------------------------------------- the real thing
@needs_fixture
def test_a_contributor_extracts_a_missing_title_and_both_devices_get_the_thumbnails(rig, caplog):
    srv, o = rig
    caplog.set_level(logging.DEBUG)
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        ctok, cdev = guest(srv, adm, "gf")
        rtok, _ = guest(srv, adm, "reader", read_only=True)
        enable(adm, cdev)
        url = _url(o, "ok_10min.mkv") + "?marker=SECRETPATHMARKER"
        base_jobs = len(srv.core.jobs.jobs)
        with srv.public_client(ctok) as g, srv.public_client(rtok) as rd:
            miss = g.post("/v1/lookup", json={"imdb_id": "tt7777777", "duration_ms": 600_000})
            assert miss.status_code == 404                                                   # not in the library yet
            r = g.post("/v1/contribute", json=contributor_body(url))
            assert r.status_code == 202, r.text
            pid = r.json()["preview_id"]
            assert adm.get("/admin/contributions").json()["jobs"][0]["device_id"] == cdev
            wait_for(lambda: g.get(f"/v1/contribute/{pid}").json()["state"] in ("ready", "complete"), 120, 0.2)
            wait_for(lambda: g.get(f"/v1/contribute/{pid}").json()["state"] == "complete", 120, 0.2)
            body = g.get(f"/v1/contribute/{pid}").json()
            assert body["progress"]["tiles_done"] == body["progress"]["tiles_total"] > 0
            vtt = httpx.get(body["vtt_url"], verify=False, timeout=30)
            assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")                  # progressive/complete timeline for HER
            tile = next(l for l in vtt.text.splitlines() if l.startswith("https://")).split("#")[0]
            assert httpx.get(tile, verify=False, timeout=30).content[:2] == bytes([0xFF, 0xD8])
            # the OTHER guest (a Reader) now finds the title in the library, and a Reader still cannot contribute
            hit = rd.post("/v1/lookup", json={"imdb_id": "tt7777777", "duration_ms": 600_000})
            assert hit.status_code == 200 and hit.json()["candidates"][0]["approximate"] is True
            assert rd.post("/v1/contribute", json=contributor_body(url)).status_code == 403
            # the owner reuses the SAME cache (no second extraction of the title: same fingerprint)
            ob = _create(owner, _url(o, "ok_10min.mkv"), at_ms=0, layout="tiles")
            wait_for(lambda: _status(owner, ob["preview_id"])["state"] == "complete", 60, 0.1)
            assert len(srv.core.cache._entries) == 1 or len(srv.core.db.q("SELECT fp FROM entries")) == 1
        m = srv.core.jobs.contributed_jobs()
        assert m and m[0]["state"] == "complete"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETPATHMARKER" not in text and "marker=" not in text                         # the submitted link is never logged


@needs_fixture
def test_a_compatible_cached_release_is_reused_without_any_extraction(rig):
    srv, o = rig
    otok, _ = _register(srv, "owner-tv")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        b = _create(owner, _url(o, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=IDS)
        wait_for(lambda: _status(owner, b["preview_id"])["state"] == "complete", 120, 0.1)
        _create(owner, _url(o, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=IDS)
        cache_ms = srv.core.cache.entry(next(iter(srv.core.jobs.sources.values())).fp).duration_ms
        ctok, cdev = guest(srv, adm)
        enable(adm, cdev)
        reqs, njobs = o.file_requests, len(srv.core.jobs.jobs)
        with srv.public_client(ctok) as g:
            r = g.post("/v1/contribute", json=contributor_body(_url(o, "other_10min.mkv"), duration_ms=cache_ms + 3000))
            assert r.status_code == 200 and r.json()["state"] == "reused" and r.json()["candidates"][0]["approximate"]
        assert o.file_requests == reqs and len(srv.core.jobs.jobs) == njobs and not srv.core.jobs.contributed_jobs()


# --------------------------------------------------------------------------------------------- unsafe input / escalation
def test_unsafe_links_are_refused_by_the_policy_and_say_nothing_about_why():
    base = DestinationPolicy(resolver=lambda h, p: ["10.0.0.5"] if "internal" in h else ["93.184.216.34"])
    pol = ContributionPolicy(base, ["torbox.app", "real-debrid.com"])
    seen = []
    pol.on_denied = seen.append
    bad = ["http://store.torbox.app/x.mkv",                        # not https
           "https://store.torbox.app:8443/x.mkv",                  # port
           "https://user:pw@store.torbox.app/x.mkv",               # userinfo
           "https://store.torbox.app.evil.example/x.mkv",          # suffix trick
           "https://evil-torbox.app/x.mkv",                        # look-alike
           "https://169.254.169.254/latest/meta-data/",            # metadata service
           "https://127.0.0.1/", "https://localhost/", "https://[::1]/", "https://0x7f000001/",
           "https://internal.torbox.app/x.mkv",                    # allow-listed name that RESOLVES to a private address (DNS rebinding / internal)
           "ftp://store.torbox.app/x", "file:///c:/windows/win.ini", "https:///x"]
    for u in bad:
        with pytest.raises(Denied):
            pol.vet(u)
    assert pol.vet("https://store.torbox.app/x.mkv")               # an allow-listed public host passes
    assert all("/" not in h for h in seen)                          # only host names are ever recorded for the owner
    assert clean_hosts(["TorBox.app", "bad host", "a..b", "x" * 300, "ok.example", "ok.example"]) == ["torbox.app", "ok.example"]


@needs_fixture
def test_privilege_escalation_and_unsafe_submissions_are_rejected_on_the_real_listeners(live_factory):
    o = Origin(ALT).start()
    try:
        port = free_port()
        srv = live_factory(_policy(o), create_wait_s=10.0, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")  # defaults: https only
        srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")}, strikes=10 ** 6))
        with srv.admin_client() as adm:
            sharing_on(adm)
            ctok, cdev = guest(srv, adm)
            enable(adm, cdev, hosts=["torbox.app"])
            otok, _ = _register(srv, "owner-tv")
            with srv.public_client(ctok) as g:
                for u in ("http://store.torbox.app/x.mkv", "https://169.254.169.254/x.mkv", "https://127.0.0.1/x.mkv", _url(o, "ok_10min.mkv")):
                    r = g.post("/v1/contribute", json=contributor_body(u))
                    assert r.status_code == 403 and r.json() == {"error": "source_not_allowed"}, u
                assert g.post("/v1/contribute", json={"source": {"url": "https://store.torbox.app/x.mkv"}, "duration_ms": 600000}).status_code == 400   # ids required
                assert g.post("/v1/contribute", json=contributor_body("https://store.torbox.app/x.mkv", duration_ms=5)).status_code == 400
                # the privileged API stays closed to a Contributor: public listener 404, LAN listener 403
                assert g.post("/v1/previews", json={"source": {"url": "https://store.torbox.app/x.mkv"}}).status_code == 404
                assert g.post("/v1/previews/" + "a" * 32 + "/priority", json={}).status_code == 404
                assert g.get("/admin/contributions").status_code == 404
            with srv.client(ctok) as lan:
                assert lan.post("/v1/previews", json={"source": {"url": "https://store.torbox.app/x.mkv"}}).status_code == 403
                assert lan.put("/v1/previews/" + "a" * 32 + "/priority", json={}).status_code in (403, 404)
            # an owner token is refused on the public listener, including on the contribution route
            with srv.public_client(otok) as owner:
                assert owner.post("/v1/contribute", json=contributor_body("https://store.torbox.app/x.mkv")).status_code == 403
            assert adm.get("/admin/contributions").json()["denied_hosts"] == ["127.0.0.1", "169.254.169.254", "media.example.com", "store.torbox.app"]  # hosts only
            assert len(srv.core.jobs.jobs) == 0                                       # nothing was ever started
    finally:
        o.stop()


# --------------------------------------------------------------------------------------------- limits, owner protection, kill
@needs_fixture
def test_one_job_per_slot_owner_preempts_and_revocation_and_kill_switch_stop_the_work(live_factory):
    o = Origin(ALT).start()
    try:
        port = free_port()
        srv = live_factory(_policy(o), pregen="full", workers_per_source=2, create_wait_s=10.0, public_port=port, public_bind="127.0.0.1",
                           public_host=f"127.0.0.1:{port}", contrib_https_only=False, contrib_mbps=4, max_active_sources=1)
        srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")}, strikes=10 ** 6))
        srv.core.contrib_spacing.n = 1000
        otok, _ = _register(srv, "owner-tv")
        with srv.client(otok) as owner, srv.admin_client() as adm:
            sharing_on(adm)
            t1, d1 = guest(srv, adm, "one")
            t2, d2 = guest(srv, adm, "two")
            enable(adm, d1)
            adm.put(f"/admin/devices/{d2}/permissions", json={"contribute": True}, headers=ADMIN)
            with srv.public_client(t1) as g1, srv.public_client(t2) as g2:
                a = g1.post("/v1/contribute", json=contributor_body(_url(o, "ok_10min.mkv")))
                assert a.status_code == 202
                pid = a.json()["preview_id"]
                assert g2.post("/v1/contribute", json=contributor_body(_url(o, "other_10min.mkv"), imdb_id="tt8888888")).status_code == 429   # global limit
                src = wait_for(lambda: next(iter(srv.core.jobs.running_sources()), None), 20, 0.1)
                assert src.contributed and src.n_workers == srv.settings.workers_per_source
                # the OWNER needs the only slot: the contribution yields, the owner is NOT refused
                ob = _create(owner, _url(o, "other_10min.mkv"), at_ms=0, layout="tiles")
                assert ob["state"] in ("preparing", "ready", "complete")
                wait_for(lambda: not any(s.contributed for s in srv.core.jobs.running_sources()), 60, 0.1)
                # a contributor can stop their own job; revoking the device or the kill switch stops the rest
                wait_wait = g1.delete(f"/v1/contribute/{pid}")
                assert wait_wait.status_code in (204, 404)
            # fresh contribution, then revoke the device
            adm.put(f"/admin/devices/{d1}/permissions", json={"contribute": True}, headers=ADMIN)
            wait_for(lambda: not srv.core.jobs.running_sources(), 120, 0.2)
            with srv.public_client(t1) as g1:
                r = g1.post("/v1/contribute", json=contributor_body(_url(o, "ok_10min.mkv"), imdb_id="tt9999999"))
                assert r.status_code in (202, 200)
                time.sleep(0.5)
                assert adm.put(f"/admin/devices/{d1}/permissions", json={"contribute": False}, headers=ADMIN).status_code == 200
                assert wait_for(lambda: not srv.core.jobs.contributed_jobs(), 10, 0.1)
                assert g1.post("/v1/contribute", json=contributor_body(_url(o, "ok_10min.mkv"))).status_code == 403
            adm.put(f"/admin/devices/{d1}/permissions", json={"contribute": True}, headers=ADMIN)
            wait_for(lambda: not srv.core.jobs.running_sources(), 120, 0.2)
            with srv.public_client(t1) as g1:
                r = g1.post("/v1/contribute", json=contributor_body(_url(o, "ok_10min.mkv"), imdb_id="tt5555555"))
                assert r.status_code in (202, 200)
                adm.put("/admin/sharing", json={"enabled": False}, headers=ADMIN)                # the kill switch
                wait_for(lambda: not srv.core.jobs.contributed_jobs(), 10, 0.1)
                assert g1.post("/v1/contribute", json=contributor_body(_url(o, "ok_10min.mkv"))).status_code == 403
    finally:
        o.stop()


def test_the_owner_hold_follows_the_owners_own_playback_guard():
    guard = lambda reason: SimpleNamespace(reason=reason)              # noqa: E731
    mk = lambda srcs: SimpleNamespace(sources=srcs)                    # noqa: E731
    calm = {"a": SimpleNamespace(contributed=False, guard=guard(None), running=True, urgent=False)}
    held = {"a": SimpleNamespace(contributed=False, guard=guard("rebuffer"), running=True, urgent=False)}
    scrub = {"a": SimpleNamespace(contributed=False, guard=None, running=True, urgent=True)}
    other = {"c": SimpleNamespace(contributed=True, guard=guard("stall"), running=True, urgent=True)}
    assert JobManager.owner_hold(mk(calm)) is False
    assert JobManager.owner_hold(mk(held)) is True and JobManager.owner_hold(mk(scrub)) is True
    assert JobManager.owner_hold(mk(other)) is False                   # a contribution's own state never holds itself


# --------------------------------------------------------------------------------------------- the REAL client code, end to end
@needs_fixture
def test_the_nuvio_client_code_drives_a_full_contribution(live_factory):
    """Pairing, permission check, request body and response handling all by the real C client code (CLI); Python is transport only."""
    from test_nuvio_client_protocol import cli as _cli_fixture  # noqa: F401
    import test_nuvio_client_protocol as nc
    import shutil, subprocess, tempfile
    from pathlib import Path
    if not nc.client_available():
        pytest.skip(nc.SKIP_MSG)
    out = Path(tempfile.mkdtemp(prefix="osc-"))
    CLIENT = nc.CLIENT
    build = [*nc.runner(), "cc", *[nc._wsl(CLIENT / "src" / x) for x in nc.SRC], nc._wsl(CLIENT / "tests/openseek_guest_cli.c"), "-I" + nc._wsl(CLIENT / "src"), "-o", nc._wsl(out / "cli"), "-O1"]
    assert subprocess.run(build, capture_output=True, text=True).returncode == 0

    def cli(*a):
        p = subprocess.run([*nc.runner(), *(["-e"] if nc.runner() else []), nc._wsl(out / "cli"), *map(str, a)], capture_output=True, text=True)
        assert p.returncode == 0, (a[:1], p.stderr)
        return p.stdout.strip()
    cli.dir = out
    o = Origin(ALT).start()
    try:
        port = free_port()
        srv = live_factory(_policy(o), pregen="full", workers_per_source=2, create_wait_s=10.0, public_port=port, public_bind="127.0.0.1",
                           public_host=f"127.0.0.1:{port}", contrib_https_only=False)
        srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")}, strikes=10 ** 6))
        srv.core.contrib_spacing.n = 1000
        with srv.admin_client() as adm:
            sharing_on(adm)
            inv = make_invite(adm, public=True, label="gf", read_only=True)
            a = nc.pair_with_client(srv, cli, inv["join"], "gf-tv", srv.public_client)
            assert a["role"] == "guest"
            enable(adm)                                                          # switch ON, but she is still a Reader
            with srv.public_client(a["token"]) as g:
                assert cli("can-contribute", g.get("/v1/pair/status").text) == "no"
                body = cli("contribute-body", "https://store.torbox.app/x.mkv", "User-Agent: Nuvio\nCookie: X", "tt4242424", 0, 0, 600000)
                assert "Cookie" not in body and "tt4242424" in body
                assert cli("contribute-body", _url(o, "ok_10min.mkv"), "", "tt4242424", 0, 0, 600000) == "refused"      # the client itself refuses http
                import json as _j
                bj = _j.loads(body)
                bj["source"]["url"] = _url(o, "ok_10min.mkv")        # the fixture origin is plain http: swap ONLY the link, keep every client-built field
                body = _j.dumps(bj)
                assert g.post("/v1/contribute", content=body, headers={"Content-Type": "application/json"}).status_code == 403
                adm.put(f"/admin/devices/{a['device']}/permissions", json={"contribute": True}, headers=ADMIN)    # the owner authorises HER device
                assert cli("can-contribute", g.get("/v1/pair/status").text) == "yes"
                r = g.post("/v1/contribute", content=body, headers={"Content-Type": "application/json"})
                assert r.status_code == 202
                pid = r.json()["preview_id"]
                wait_for(lambda: g.get(f"/v1/contribute/{pid}").json()["state"] == "complete", 120, 0.2)
                vtt = httpx.get(g.get(f"/v1/contribute/{pid}").json()["vtt_url"], verify=False, timeout=30)
                assert vtt.status_code == 200 and "-->" in vtt.text
                look = g.post("/v1/lookup", content=cli("lookup-body", "tt4242424", 0, 0, 600000, 0), headers={"Content-Type": "application/json"})
                assert look.status_code == 200                                     # now in the library for everybody
    finally:
        o.stop()


# --------------------------------------------------------------------------------------------- default access, work-conserving scheduling
def test_invited_guests_are_contributors_by_default_but_read_only_is_explicit_and_old_guests_stay_readers(live_factory):
    port = free_port()
    srv = live_factory(None, public_port=port, public_bind="127.0.0.1", public_host=f"127.0.0.1:{port}")
    with srv.admin_client() as adm:
        sharing_on(adm)
        a = make_invite(adm, public=True, label="trusted")
        b = make_invite(adm, public=True, label="viewer", read_only=True)
        assert a["contribute"] is True and b["contribute"] is False
        da = pjoin(srv, a["invite_id"], a["code"], "a").json()["device_id"]
        db_ = pjoin(srv, b["invite_id"], b["code"], "b").json()["device_id"]
        devs = {d["device_id"]: d for d in adm.get("/admin/status").json()["devices"]}
        assert devs[da]["contribute"] is True and devs[db_]["contribute"] is False
        # the host's switch is still the master: with contributions OFF even the default Contributor is refused
        assert adm.get("/admin/contributions").json()["effective"] is False and srv.core.can_contribute(da) is False
        adm.put("/admin/contributions", json={"enabled": True, "hosts": [HOST]}, headers=ADMIN)
        assert srv.core.can_contribute(da) is True and srv.core.can_contribute(db_) is False
        # a device that existed before (contribute flag 0, the migration default) stays a Reader after the switch is turned on
        srv.core.db.x("UPDATE devices SET contribute=0 WHERE device_id=?", (da,))
        assert srv.core.can_contribute(da) is False
        adm.put(f"/admin/devices/{da}/permissions", json={"contribute": True}, headers=ADMIN)
        assert srv.core.can_contribute(da) is True


def test_contribution_pacing_is_work_conserving_and_owner_first():
    from openseek.jobs import SharedShare
    calls = []

    class Rec:
        def __init__(self, name):
            self.name = name

        def throttle(self, n, active, urgent=None):
            calls.append((self.name, n))
    busy = [False]
    sh = SharedShare(Rec("global"), Rec("floor"), lambda: busy[0])
    sh.throttle(1000, lambda: True)
    busy[0] = True
    sh.throttle(1000, lambda: True)
    assert calls == [("global", 1000), ("global", 1000), ("floor", 1000)]      # idle: the global cap only; busy: also the smaller floor
    mk = lambda **kw: SimpleNamespace(**kw)                                  # noqa: E731
    gv = lambda age: SimpleNamespace(guard_view=lambda: {"age": age})        # noqa: E731

    def busy_of(*srcs):
        return JobManager.owner_busy(SimpleNamespace(sources={i: x for i, x in enumerate(srcs)}))
    assert busy_of() is False
    assert busy_of(mk(contributed=False, running=True, governor=gv(None))) is True
    assert busy_of(mk(contributed=False, running=False, governor=gv(3.0))) is True       # playing (telemetry 3 s old)
    assert busy_of(mk(contributed=False, running=False, governor=gv(120.0))) is False    # telemetry long gone
    assert busy_of(mk(contributed=True, running=True, governor=gv(1.0))) is False        # a contribution never makes the owner "busy"


@needs_fixture
def test_idle_contributor_gets_the_owners_workers_preemption_keeps_progress_and_telemetry_guards_her_job(live_factory):
    o = Origin(ALT).start()
    try:
        port = free_port()
        srv = live_factory(_policy(o), pregen="full", workers_per_source=2, create_wait_s=10.0, public_port=port, public_bind="127.0.0.1",
                           public_host=f"127.0.0.1:{port}", contrib_https_only=False, contrib_mbps=4, max_active_sources=1, playback_guard=True)
        srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")}, strikes=10 ** 6))
        srv.core.contrib_spacing.n = 1000
        otok, _ = _register(srv, "owner-tv")
        with srv.client(otok) as owner, srv.admin_client() as adm:
            sharing_on(adm)
            tok, did = guest(srv, adm, "gf")
            enable(adm)
            with srv.public_client(tok) as g:
                r = g.post("/v1/contribute", json=contributor_body(_url(o, "ok_10min.mkv"), imdb_id="tt3131313"))
                assert r.status_code == 202
                pid = r.json()["preview_id"]
                src = wait_for(lambda: next(iter(srv.core.jobs.running_sources()), None), 20, 0.1)
                assert src.contributed and src.n_workers == 2 and len(src.engines) == 2     # idle: the OWNER's worker count, not 1
                # her own playback health reaches the guard of HER job (and only hers)
                before = src.governor.guard_view()["reports"]
                assert g.post(f"/v1/contribute/{pid}/telemetry", json={"buffer_ms": 800, "rebuffering": True, "host": "cdn.example", "conns": 1}).status_code == 204
                assert src.governor.guard_view()["reports"] == before + 1
                assert g.post("/v1/contribute/" + "b" * 32 + "/telemetry", json={"buffer_ms": 1, "rebuffering": False}).status_code == 404
                for _ in range(3):                                                                   # ... until her player reports it healthy again
                    assert g.post(f"/v1/contribute/{pid}/telemetry", json={"buffer_ms": 30000, "rebuffering": False, "host": "cdn.example", "conns": 1}).status_code == 204
                tiles_before = len(srv.core.db.q("SELECT k FROM tiles"))
                # the owner needs the only slot: the contribution is preempted but what it cached stays
                ob = _create(owner, _url(o, "other_10min.mkv"), at_ms=0, layout="tiles")
                wait_for(lambda: not any(s.contributed for s in srv.core.jobs.running_sources()), 60, 0.1)
                assert len(srv.core.db.q("SELECT k FROM tiles")) >= tiles_before
                wait_for(lambda: _status(owner, ob["preview_id"])["state"] == "complete", 120, 0.2)
                # slot free again: her next status poll resumes the SAME job from its cached tiles and it completes
                def done():                                                  # her player keeps reporting a healthy buffer while she watches
                    g.post(f"/v1/contribute/{pid}/telemetry", json={"buffer_ms": 30000, "rebuffering": False, "host": "cdn.example", "conns": 1})
                    return g.get(f"/v1/contribute/{pid}").json()["state"] == "complete"
                wait_for(done, 150, 0.7)
    finally:
        o.stop()
