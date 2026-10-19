"""Desktop app support: identify (tag) and delete ONE cache entry through the loopback admin API."""
from __future__ import annotations

import time

ADMIN = {"X-OpenSeek-Admin": "1"}
FP = "ab" + "c" * 62


def test_tag_and_delete_one_entry_without_touching_others(live_factory):
    srv = live_factory(None)
    db, root = srv.core.db, srv.core.cache.root
    now = time.time()
    for fp in (FP, "cd" + "e" * 62):
        db.x("INSERT INTO entries(fp,duration_ms,n_tiles,version,bytes,created,last_used) VALUES(?,?,?,?,?,?,?)", (fp, 600000, 60, 1, 1000, now, now))
        d = root / fp[:2] / fp / "tiles"
        d.mkdir(parents=True)
        (d / "0.jpg").write_bytes(b"\xff\xd8x")
    with srv.admin_client() as a:
        assert a.put(f"/admin/library/entries/{FP}", json={"imdb_id": "tt0994314", "season": 1, "episode": 2}).json()["imdb_id"] == "tt0994314"
        assert db.q("SELECT imdb_id,season,episode FROM release_meta WHERE fp=?", (FP,)) == [("tt0994314", 1, 2)]
        assert a.put(f"/admin/library/entries/{FP}", json={"imdb_id": "nope"}).status_code == 400
        assert a.put("/admin/library/entries/" + "0" * 64, json={"imdb_id": "tt1"}).status_code == 404
        assert a.put("/admin/library/entries/not-a-fingerprint", json={"imdb_id": "tt1"}).status_code == 404
        assert (root / FP[:2] / FP / "tiles" / "0.jpg").exists()                           # tagging never touches files
        assert a.delete(f"/admin/library/entries/{FP}").status_code == 204
        assert not (root / FP[:2] / FP).exists() and db.q("SELECT COUNT(*) FROM entries WHERE fp=?", (FP,))[0][0] == 0
        assert (root / "cd" / ("cd" + "e" * 62) / "tiles" / "0.jpg").exists()              # the other entry is intact
        assert a.delete(f"/admin/library/entries/{FP}").status_code == 404
        assert a.delete("/admin/library/entries/..%2f..").status_code in (404, 400)
