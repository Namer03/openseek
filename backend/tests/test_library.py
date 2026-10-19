"""Stage 1: cross-release thumbnail reuse. Two different encodes of one title; the second reuses the first's cache."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from helpers import FPS, decode_barcode, parse_vtt, wait_for
from origin import Origin
from openseek.library import clean_ids, relation, release_key, to_guest_time
from openseek.vtt import render
from test_integration import _create, _policy, _status, _url
from test_scenarios import _register

from helpers import FIXTURES as ALT, FIXTURES_HINT
needs_fixture = pytest.mark.skipif(not (ALT / "ok_10min.mkv").exists(), reason=FIXTURES_HINT)
ADMIN = {"X-OpenSeek-Admin": "1"}
HINTS = {"imdb_id": "tt1856101", "season": 0, "episode": 0}


# ------------------------------------------------------------------------------------------------ units
def test_ids_are_strictly_validated():
    assert clean_ids("tt1856101", None, 0, 0).imdb == "tt1856101"
    assert clean_ids(None, 335984, 2, 3).tmdb == 335984
    for bad in ("", "1856101", "tt", "tt12345678901", "tt1856101; DROP", None):
        assert clean_ids(bad, None, 0, 0) is None
    assert clean_ids(None, 0, 0, 0) is None and clean_ids(None, True, 0, 0) is None
    assert clean_ids("tt1", None, -1, 10 ** 6).season == 0                   # nonsense numbers become 0, never stored raw


def test_same_size_is_not_the_same_release_and_nothing_is_ever_verified_by_duration():
    a = release_key(clean_ids("tt1", None, 0, 0), 600_000, 1_000_000)
    assert a != release_key(clean_ids("tt1", None, 0, 0), 601_500, 1_000_000)      # another duration => another release key
    assert a == release_key(clean_ids("tt1", None, 0, 0), 600_100, 1_000_000)      # rounded to the second
    assert relation(600_000, 600_000)[0] == "same_length"
    assert relation(600_000, 607_000)[0] == "length_differs"
    assert relation(576_000, 600_000)[0] == "speed_adjusted" and relation(576_000, 600_000)[1] == pytest.approx(600 / 576)
    assert relation(600_000, 9_000_000) is None


def test_vtt_remap_shifts_scales_and_clips_to_the_guest_timeline():
    tiles = {k: k * 10_000 for k in range(6)}
    plain = render("https://h", "c" * 32, "p" * 32, 1, "tiles", tiles, 60_000, 6, 5000)
    shifted = render("https://h", "c" * 32, "p" * 32, 1, "tiles", tiles, 60_000, 6, 5000, (1.0, -7000.0, 67_000))
    p, s = parse_vtt(plain), parse_vtt(shifted)
    assert len(p) == len(s) == 6
    assert s[1][0] - p[1][0] == 7000 and s[1][1] - p[1][1] == 7000               # guest time = cache time + 7 s
    clipped = render("https://h", "c" * 32, "p" * 32, 1, "tiles", tiles, 60_000, 6, 5000, (1.0, 0.0, 30_000))
    assert max(e for _, e, _ in parse_vtt(clipped)) <= 30_000                    # nothing beyond the guest's own duration
    assert to_guest_time(60_000, 1.0417, 0) == pytest.approx(57_600, abs=5)


# ------------------------------------------------------------------------------------------------ the demonstration
def _vtt(c, url):
    r = c.get(url, headers={"Authorization": ""})
    assert r.status_code == 200, r.text
    return parse_vtt(r.text)


def _tile_film_ms(c, cue_url) -> int:
    path = urlsplit(cue_url)
    r = c.get(cue_url.split("#")[0], headers={"Authorization": ""})
    assert r.status_code == 200
    frame = decode_barcode(r.content)
    assert frame is not None
    return int(frame * 1000 / FPS)


def _shared_state(srv) -> str:
    """Everything that is shared library content, hashed: tiles, entries and the metadata index."""
    con = sqlite3.connect(str(srv.core.db.path))
    h = hashlib.sha256()
    for t in ("tiles", "entries", "release_meta"):
        h.update(repr(sorted(con.execute(f"SELECT * FROM {t}").fetchall())).encode())
    con.close()
    return h.hexdigest()


@needs_fixture
def test_a_cached_timeline_is_reused_by_a_different_release_without_any_extraction(live_factory):
    origin = Origin(ALT).start()
    try:
        srv = live_factory(_policy(origin), pregen="full", workers_per_source=2, create_wait_s=10.0)
        tok1, _ = _register(srv, "owner-tv")
        tok2, dev2 = _register(srv, "second-device")
        with srv.client(tok1) as c1, srv.client(tok2) as c2, srv.admin_client() as adm:
            # --- off by default: nothing is stored, lookups are refused
            assert adm.get("/admin/library").json()["metadata_index"] is False
            body = _create(c1, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
            wait_for(lambda: _status(c1, body["preview_id"])["state"] == "complete", 120, 0.1)
            assert adm.get("/admin/library/releases").json()["releases"] == []
            guest = {"imdb_id": "tt1856101", "duration_ms": 0}
            src = next(iter(srv.core.jobs.sources.values()))
            cache_ms = srv.core.cache.entry(src.fp).duration_ms
            guest = {"imdb_id": "tt1856101", "season": 0, "episode": 0, "duration_ms": cache_ms + 7000, "layout": "tiles"}
            assert c1.post("/v1/lookup", json=guest).status_code == 403

            # --- the owner opts in; the title is recorded the next time it is played (ids + observed size only)
            assert adm.put("/admin/library", json={"metadata_index": True}, headers=ADMIN).json()["metadata_index"] is True
            _create(c1, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
            rows = adm.get("/admin/library/releases").json()["releases"]
            assert len(rows) == 1 and rows[0]["imdb_id"] == "tt1856101" and rows[0]["tiles"] == rows[0]["n_tiles"]
            assert set(rows[0]) == {"fp", "imdb_id", "tmdb_id", "season", "episode", "size_bytes", "duration_ms", "tiles",
                                    "n_tiles", "last_used"}                       # nothing about titles, files or URLs
            assert rows[0]["size_bytes"] == (ALT / "ok_10min.mkv").stat().st_size

            # --- a different release (the guest's own file: 7 s of extra intro) looks the library up
            reqs, nsrc, njobs = origin.file_requests, len(srv.core.jobs.sources), len(srv.core.jobs.jobs)
            state = _shared_state(srv)
            r = c2.post("/v1/lookup", json=guest)
            assert r.status_code == 200, r.text
            cand = r.json()["candidates"][0]
            assert cand["kind"] == "length_differs" and cand["approximate"] is True and cand["verified"] is False
            assert cand["source"]["coverage"] == 1.0 and "ok_10min" not in json.dumps(r.json())     # no URL, no file name
            cues = _vtt(c2, cand["vtt_url"])
            assert len(cues) >= 55

            # default mapping: right movie, wrong place (7 s) - the very reason the approximation is flagged
            errs = [abs((s + e) // 2 - (_tile_film_ms(c2, u) + 7000)) for s, e, u in cues[10:14]]
            assert min(errs) >= 5500, "an un-adjusted shared timeline must be visibly off for this release"

            # --- the device corrects it; the correction is remembered for this release and applies to the VTT
            assert c2.put(f"/v1/previews/{cand['preview_id']}/offset", json={"offset_ms": -7000}).status_code == 200
            fixed = _vtt(c2, cand["vtt_url"])
            errs = [abs((s + e) // 2 - (_tile_film_ms(c2, u) + 7000)) for s, e, u in fixed[10:30]]
            assert max(errs) <= 5000, f"offset-corrected cues must land within tolerance, got {max(errs)} ms"
            again = c2.post("/v1/lookup", json=guest).json()["candidates"][0]
            assert again["preview_id"] == cand["preview_id"] and again["kind"] == "user_adjusted" and again["mapping"]["b"] == -7000

            # --- per-device: the other device is unaffected by it, and by a rejection from the first one
            mine = c1.post("/v1/lookup", json=guest).json()["candidates"][0]
            assert mine["kind"] == "length_differs" and mine["mapping"]["b"] == 0 and mine["preview_id"] != cand["preview_id"]
            assert c1.post(f"/v1/previews/{mine['preview_id']}/reject").status_code == 204
            assert c1.post("/v1/lookup", json=guest).status_code == 404                 # rejected for device 1 only
            assert c2.post("/v1/lookup", json=guest).status_code == 200
            assert c1.put(f"/v1/previews/{cand['preview_id']}/offset", json={"offset_ms": 5}).status_code == 404   # not theirs
            assert c1.post(f"/v1/previews/{cand['preview_id']}/reject").status_code == 404
            assert c2.delete(f"/v1/previews/{cand['preview_id']}/offset").status_code == 204        # reset to the default mapping

            # --- nothing was extracted, fetched or changed in the shared library by any of that
            assert origin.file_requests == reqs, "lookup/borrow/offset/reject must not touch the CDN"
            assert (len(srv.core.jobs.sources), len(srv.core.jobs.jobs)) == (nsrc, njobs)
            assert _shared_state(srv) == state

            # --- unknown titles are simply not cached; the client keeps using its own path
            assert c1.post("/v1/lookup", json={**guest, "imdb_id": "tt9999999"}).status_code == 404
            assert c1.post("/v1/lookup", json={**guest, "imdb_id": "bogus"}).status_code == 400
            assert c1.post("/v1/lookup", json={"duration_ms": 0}).status_code == 400

            # --- opting out stops lookups and recording; the owner can purge what is stored
            adm.put("/admin/library", json={"metadata_index": False}, headers=ADMIN)
            assert c2.post("/v1/lookup", json=guest).status_code == 403
            assert adm.delete("/admin/library/releases", headers=ADMIN).json()["purged"] == 1
            assert adm.get("/admin/library/releases").json()["releases"] == []
    finally:
        origin.stop()


@needs_fixture
def test_a_24_25_speed_conversion_is_scaled_and_still_flagged_approximate(live_factory):
    origin = Origin(ALT).start()
    try:
        srv = live_factory(_policy(origin), pregen="full", workers_per_source=2, create_wait_s=10.0)
        tok, _ = _register(srv, "tv")
        with srv.client(tok) as c, srv.admin_client() as adm:
            adm.put("/admin/library", json={"metadata_index": True}, headers=ADMIN)
            body = _create(c, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints=HINTS)
            wait_for(lambda: _status(c, body["preview_id"])["state"] == "complete", 120, 0.1)
            src = next(iter(srv.core.jobs.sources.values()))
            cache_ms = srv.core.cache.entry(src.fp).duration_ms
            guest_ms = int(cache_ms * 24 / 25)                                         # the PAL release plays faster
            res = c.post("/v1/lookup", json={"imdb_id": "tt1856101", "duration_ms": guest_ms, "layout": "tiles"})
            cand = res.json()["candidates"][0]
            assert cand["kind"] == "speed_adjusted" and cand["approximate"] and not cand["verified"]
            assert cand["mapping"]["a"] == pytest.approx(25 / 24, rel=2e-3)
            cues = _vtt(c, cand["vtt_url"])
            errs = [abs((s + e) // 2 - int(_tile_film_ms(c, u) * 24 / 25)) for s, e, u in cues[5:40]]
            assert max(errs) <= 5000
            assert max(e for _, e, _ in cues) <= guest_ms                                # never beyond the guest's own length
    finally:
        origin.stop()


@needs_fixture
def test_the_library_never_matches_a_different_title_or_episode(live_factory):
    origin = Origin(ALT).start()
    try:
        srv = live_factory(_policy(origin), pregen="off", workers_per_source=1, create_wait_s=10.0)
        tok, _ = _register(srv, "tv")
        with srv.client(tok) as c, srv.admin_client() as adm:
            adm.put("/admin/library", json={"metadata_index": True}, headers=ADMIN)
            _create(c, _url(origin, "ok_10min.mkv"), at_ms=0, layout="tiles", hints={"imdb_id": "tt0944947", "season": 1, "episode": 2})
            wait_for(lambda: any(s.scheduler and s.scheduler.n for s in srv.core.jobs.sources.values()), 30, 0.1)
            src = next(iter(srv.core.jobs.sources.values()))
            wait_for(lambda: len(srv.core.cache.entry(src.fp).tiles) >= 1, 30, 0.1)
            dur = srv.core.cache.entry(src.fp).duration_ms
            base = {"imdb_id": "tt0944947", "duration_ms": dur}
            assert c.post("/v1/lookup", json={**base, "season": 1, "episode": 2}).status_code == 200
            for other in ({"season": 1, "episode": 3}, {"season": 2, "episode": 2}, {"season": 0, "episode": 0}):
                assert c.post("/v1/lookup", json={**base, **other}).status_code == 404
            assert c.post("/v1/lookup", json={"imdb_id": "tt0000001", "season": 1, "episode": 2, "duration_ms": dur}).status_code == 404
    finally:
        origin.stop()
