"""Stage 3: two client identities against the isolated backend, with every protocol decision made by the REAL
Nuvio client code (src/openseekproto.c + osp1.c of the separate client repo, built as tests/openseek_guest_cli.c;
set OPENSEEK_NUVIO_CLIENT_DIR to a checkout of it, otherwise these tests skip). Python is only
the HTTP transport here, so a mismatch between the C client and the server contract fails this test."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from helpers import wait_for
from openseek import pairing
from test_integration import _create, _status, _url
from test_scenarios import _register
from test_sharing import ALT, HINTS, anon, make_invite, needs_fixture, rig, sharing_on  # noqa: F401  (rig is a fixture)

CLIENT = Path(os.environ.get("OPENSEEK_NUVIO_CLIENT_DIR") or "-no-client-checkout-")
SKIP_MSG = ("separate Nuvio client checkout not available: set OPENSEEK_NUVIO_CLIENT_DIR to a directory containing "
            "tests/openseek_guest_cli.c (needs WSL or a native C compiler)")


def client_available() -> bool:
    if not os.environ.get("OPENSEEK_NUVIO_CLIENT_DIR") or not (CLIENT / "tests/openseek_guest_cli.c").exists():
        return False
    return bool(shutil.which("wsl")) if os.name == "nt" else bool(shutil.which("cc"))


def runner() -> list[str]:
    return ["wsl"] if os.name == "nt" else []


SRC = ["openseekproto.c", "osp1.c", "seekrvtt.c", "js.c", "redeurl.c"]


def _wsl(p: Path | str) -> str:
    p = str(p).replace("\\", "/")
    return f"/mnt/{p[0].lower()}{p[2:]}" if len(p) > 1 and p[1] == ":" else p


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    monkeypatch.setattr(pairing, "ITERATIONS", 200_000)


@pytest.fixture(scope="module")
def cli():
    if not client_available():
        pytest.skip(SKIP_MSG)
    out = Path(tempfile.mkdtemp(prefix="osg-"))
    build = [*runner(), "cc", *[_wsl(CLIENT / "src" / s) for s in SRC], _wsl(CLIENT / "tests/openseek_guest_cli.c"),
             "-I" + _wsl(CLIENT / "src"), "-o", _wsl(out / "osg-cli"), "-O1"]
    r = subprocess.run(build, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]

    def run(*args: str) -> str:
        p = subprocess.run([*runner(), *(["-e"] if runner() else []), _wsl(out / "osg-cli"), *map(str, args)], capture_output=True, text=True)
        assert p.returncode == 0, (args[:1], p.stdout, p.stderr)
        return p.stdout.strip()

    run.dir = out
    yield run
    shutil.rmtree(out, ignore_errors=True)


def kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def pair_with_client(srv, cli, typed: str, name: str, mk=None) -> dict:
    """What openseek_parear + parearFio do, step for step, with the C client making each decision."""
    ent = kv(cli("entrada", typed))
    assert ent["forma"] == "2", ent
    with (mk() if mk else anon(srv)) as c:
        body = cli("start-body", ent["convite"])
        s = c.post("/v1/pair/start", content=body, headers={"Content-Type": "application/json"})
        if s.status_code != 200:
            return {"http": s.status_code, "stage": "start"}
        sj = cli.dir / f"{name}-start.json"
        sj.write_text(s.text)
        claim = cli("claim", ent["segredo"], _wsl(sj), name)
        r = c.post("/v1/pair/claim", content=claim, headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            return {"http": r.status_code, "stage": "claim"}
        cj = cli.dir / f"{name}-claim.json"
        cj.write_text(r.text)
        fin = cli("finish", ent["segredo"], _wsl(sj), _wsl(cj), name)
        assert fin.startswith("ok"), fin           # the client verified the SERVER's proof (mutual authentication)
        f = kv(fin.replace("ok ", "").replace(" ", "\n"))
        return {"http": 200, "role": f["role"], "token": f["token"], "device": f["device"], "pin": s.json()["spki_pin"]}


def lookup(srv, cli, g: httpx.Client, imdb: str, dur: int, size: int = 0):
    r = g.post("/v1/lookup", content=cli("lookup-body", imdb, 0, 0, dur, size), headers={"Content-Type": "application/json"})
    return r


@needs_fixture
def test_two_nuvio_identities_share_one_cached_title_end_to_end(rig, cli):
    srv, origin = rig
    otok, _ = _register(srv, "owner-nuvio")
    with srv.client(otok) as owner, srv.admin_client() as adm:
        sharing_on(adm)
        body = _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
        _create(owner, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
        cache_ms = srv.core.cache.entry(next(iter(srv.core.jobs.sources.values())).fp).duration_ms
        reqs, nsrc, njobs = origin.file_requests, len(srv.core.jobs.sources), len(srv.core.jobs.jobs)

        # identity 1: joins by the join string (what a pasted invitation is); identity 2: by the 16 typed characters
        inv1 = make_invite(adm, label="guest-A")
        inv2 = make_invite(adm, label="guest-B")
        base = srv.base
        join_str = inv1["join"]
        ent = kv(cli("entrada", join_str))
        assert ent["forma"] == "2" and ent["convite"] == inv1["invite_id"] and ent["pino"] and ent["base"].startswith("https://") and ent["base"].endswith(":" + base.rsplit(":", 1)[1])
        a = pair_with_client(srv, cli, join_str, "guest-a")
        b = pair_with_client(srv, cli, inv2["code"], "guest-b")
        assert a["role"] == "guest" and b["role"] == "guest" and a["token"] != b["token"]
        # the client's certificate-pin decision: the invite pin and the server's start pin are the same
        assert ent["pino"].replace("%2F", "/") == a["pin"] or ent["pino"] == a["pin"]
        # a wrong code is refused by the SERVER proof check, not accepted by the client
        bad = make_invite(adm, label="bad")
        wrong = bad["code"][:-1] + ("2" if bad["code"][-1] != "2" else "3")      # one wrong symbol in the secret
        assert pair_with_client(srv, cli, wrong, "bad").get("http") != 200

        dur = cache_ms + 7000                        # a different release (7 s longer)
        with srv.client(a["token"]) as ga, srv.client(b["token"]) as gb:
            ra, rb = lookup(srv, cli, ga, "tt1856101", dur), lookup(srv, cli, gb, "tt1856101", dur)
            assert ra.status_code == 200 and rb.status_code == 200
            ma = kv(cli("parse-lookup", base, ra.text))
            mb = kv(cli("parse-lookup", base, rb.text))
            assert ma["aprox"] == "1" and ma["verif"] == "0" and "verific" not in ma["rotulo"].lower().replace("não verific", "")
            assert ma["id"] != mb["id"]

            # borrowed VTT + a real JPEG, through the capability URL the client accepted (paired origin only)
            with anon(srv) as plain:
                vtt = plain.get(ma["vtt"])
                assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")
                jpg = next(l for l in vtt.text.splitlines() if l.startswith("https://")).split("#")[0]
                assert plain.get(jpg).content[:2] == b"\xff\xd8"
                first = [l for l in vtt.text.splitlines() if "-->" in l][1]

            # manual sync: the client computes the new offset, the server stores it, and the timeline moves
            newb = int(cli("offset-after", ma["b"], 5000))          # "5 s later" -> b - 5000
            assert newb == int(float(ma["b"])) - 5000
            r = ga.put(f"/v1/previews/{ma['id']}/offset", content=cli("offset-body", newb), headers={"Content-Type": "application/json"})
            assert r.status_code == 200, r.text
            with anon(srv) as plain:
                moved = plain.get(kv(cli("parse-lookup", base, lookup(srv, cli, ga, "tt1856101", dur).text))["vtt"])
                assert [l for l in moved.text.splitlines() if "-->" in l][1] != first
            m2 = kv(cli("parse-lookup", base, lookup(srv, cli, ga, "tt1856101", dur).text))
            assert m2["tipo"] == "user_adjusted" and "ajust" in m2["rotulo"].lower()
            # B is untouched by A's offset
            assert kv(cli("parse-lookup", base, lookup(srv, cli, gb, "tt1856101", dur).text))["b"] == mb["b"]

            # undo, then reject ("wrong video"): hidden for A only
            assert ga.delete(f"/v1/previews/{ma['id']}/offset").status_code in (200, 204)
            assert ga.post(f"/v1/previews/{ma['id']}/reject", content="{}", headers={"Content-Type": "application/json"}).status_code in (200, 204)
            assert lookup(srv, cli, ga, "tt1856101", dur).status_code == 404
            assert lookup(srv, cli, gb, "tt1856101", dur).status_code == 200

            # the client refuses to even build a lookup without an IMDb id, and never has a field for a video URL
            assert cli("lookup-body", "not-an-id", 0, 0, dur, 0) == "refused"
            assert "url" not in cli("lookup-body", "tt1856101", 0, 0, dur, 123).lower()

            # guest actions that would extract or administer are refused
            assert gb.post("/v1/previews", json={"source": {"url": _url(origin, "other_10min.mkv")}, "layout": "tiles"}).status_code == 403
            assert gb.get("/admin/status").status_code in (403, 404)

            # nothing above cost the owner's server an extraction job, a source, or one request to the CDN
            assert origin.file_requests == reqs and len(srv.core.jobs.sources) == nsrc and len(srv.core.jobs.jobs) == njobs

            # revocation: the next request fails for B, and so does the URL it already held; A (same owner) unaffected
            assert adm.delete(f"/admin/devices/{b['device']}", headers={"X-OpenSeek-Admin": "1"}).status_code == 204
            assert lookup(srv, cli, gb, "tt1856101", dur).status_code == 401
            with anon(srv) as plain:
                assert plain.get(mb["vtt"]).status_code == 403
            assert origin.file_requests == reqs and len(srv.core.jobs.jobs) == njobs


@needs_fixture
def test_two_nuvio_identities_through_the_public_listener(live_factory, cli):
    """The acceptance demonstration again, but every request goes through the internet-facing listener and the invitations
    advertise the PUBLIC host:port, parsed by the real client code."""
    from helpers import free_port
    from test_integration import _policy
    from origin import Origin
    from test_public_listener import PublicLimiter, PublicLimits
    o = Origin(ALT).start()
    try:
        port = free_port()
        host = f"127.0.0.1:{port}"
        srv = live_factory(_policy(o), pregen="full", workers_per_source=2, create_wait_s=10.0,
                           public_port=port, public_bind="127.0.0.1", public_host=host)
        srv.core.public_limiter = PublicLimiter(PublicLimits(per_minute={k: 10_000 for k in ("health", "pair", "api", "cap", "unknown")}, strikes=10 ** 6))
        otok, _ = _register(srv, "owner-nuvio")
        with srv.client(otok) as owner, srv.admin_client() as adm:
            sharing_on(adm)
            body = _create(owner, _url(o, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
            wait_for(lambda: _status(owner, body["preview_id"])["state"] == "complete", 120, 0.1)
            _create(owner, _url(o, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
            cache_ms = srv.core.cache.entry(next(iter(srv.core.jobs.sources.values())).fp).duration_ms
            reqs, njobs = o.file_requests, len(srv.core.jobs.jobs)
            inv1, inv2 = make_invite(adm, public=True, label="A"), make_invite(adm, public=True, label="B")
            ent = kv(cli("entrada", inv1["join"]))                                  # the real client parses the public name:port
            assert ent["forma"] == "2" and ent["base"] == f"https://{host}", ent
            a = pair_with_client(srv, cli, inv1["join"], "gf-a", srv.public_client)
            b = pair_with_client(srv, cli, inv2["code"], "gf-b", srv.public_client)
            assert a["role"] == b["role"] == "guest"
            base, dur = f"https://{host}", cache_ms + 7000
            def look(g):
                return g.post("/v1/lookup", content=cli("lookup-body", "tt1856101", 0, 0, dur, 0), headers={"Content-Type": "application/json"})
            with srv.public_client(a["token"]) as ga, srv.public_client(b["token"]) as gb:
                ra = look(ga); assert ra.status_code == 200
                m = kv(cli("parse-lookup", base, ra.text))                            # refused unless the VTT is on the paired origin
                assert m["aprox"] == "1" and m["verif"] == "0" and m["vtt"].startswith(base + "/v1/c/")
                vtt = httpx.get(m["vtt"], verify=False)
                assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")
                tile = next(l for l in vtt.text.splitlines() if l.startswith("https://")).split("#")[0]
                assert httpx.get(tile, verify=False).content[:2] == bytes([0xFF, 0xD8])
                first = [l for l in vtt.text.splitlines() if "-->" in l][1]
                nb = int(cli("offset-after", m["b"], 5000))
                assert ga.put(f"/v1/previews/{m['id']}/offset", content=cli("offset-body", nb), headers={"Content-Type": "application/json"}).status_code == 200
                moved = httpx.get(kv(cli("parse-lookup", base, look(ga).text))["vtt"], verify=False)
                assert [l for l in moved.text.splitlines() if "-->" in l][1] != first
                assert gb.post("/v1/previews", json={"source": {"url": _url(o, "other_10min.mkv")}, "layout": "tiles"}).status_code == 404
                assert o.file_requests == reqs and len(srv.core.jobs.jobs) == njobs
                assert adm.delete(f"/admin/devices/{b['device']}", headers={"X-OpenSeek-Admin": "1"}).status_code == 204
                assert look(gb).status_code == 401
                assert look(ga).status_code == 200
    finally:
        o.stop()
