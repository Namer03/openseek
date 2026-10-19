"""Owner vs Contributor extraction benchmark on the SAME real HTTPS media source, on an ISOLATED server with production settings.

    python bench_contrib.py [url] [rounds]

Each round: (1) owner extraction (POST /v1/previews), delete the cache entry, (2) contributor extraction (POST /v1/contribute), delete the
entry. Runs alternate to even out network variance. Reports wall time, thumbnails/min, bytes and CDN requests (from the server's own
metrics), and the highest number of simultaneous workers sampled. Never touches the production server or its data."""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from openseek import pairing  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
ADMIN = {"X-OpenSeek-Admin": "1"}


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    data = Path(tempfile.mkdtemp(prefix="osbench-"))
    api, adm_p, pub = free_port(), free_port(), free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENSEEK_")}
    env.update(OPENSEEK_DATA_DIR=str(data), OPENSEEK_HOST="127.0.0.1", OPENSEEK_PORT=str(api), OPENSEEK_ADMIN_PORT=str(adm_p),
               OPENSEEK_PUBLIC_PORT=str(pub), OPENSEEK_PUBLIC_BIND="127.0.0.1", OPENSEEK_PUBLIC_HOST=f"127.0.0.1:{pub}",
               # the settings of the real production service (tools/run_production.ps1)
               OPENSEEK_BG_MBPS="64", OPENSEEK_PREGEN="full", OPENSEEK_TOLERANCE_S="5", OPENSEEK_UPSTREAM_RPS="3",
               OPENSEEK_WORKERS_PER_SOURCE="2", OPENSEEK_MULTIRANGE_PREFETCH="1", OPENSEEK_MULTIRANGE_BATCH="8",
               OPENSEEK_PLAYBACK_GUARD="1")
    p = subprocess.Popen([sys.executable, "-m", "openseek", "serve"], cwd=Path(__file__).resolve().parent.parent, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        adm = httpx.Client(base_url=f"http://127.0.0.1:{adm_p}", headers=ADMIN, timeout=30)
        for _ in range(150):
            try:
                if adm.get("/admin/status").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        host = httpx.URL(URL).host
        adm.put("/admin/sharing", json={"enabled": True})
        adm.put("/admin/library", json={"metadata_index": True})
        adm.put("/admin/contributions", json={"enabled": True, "hosts": [host.split(".", 1)[-1] if host.count(".") > 1 else host]})
        # owner (LAN API) and contributor (public listener) through the real OSP1 flows
        code = adm.post("/admin/pair").json()["code"]
        with httpx.Client(base_url=f"https://127.0.0.1:{api}", verify=False, timeout=60) as c:
            st = c.post("/v1/pair/start", json={}).json()
            k = pairing.derive_key(code, st["pair_id"], st["salt"], st["spki_pin"], st["iterations"])
            ot = c.post("/v1/pair/claim", json={"pair_id": st["pair_id"], "device_name": "owner", "client_proof": pairing.client_proof(k, st["pair_id"], "owner")}).json()["token"]
        inv = adm.post("/admin/invites", json={"public": True, "label": "bench"}).json()
        with httpx.Client(base_url=f"https://127.0.0.1:{pub}", verify=False, timeout=60) as c:
            s = c.post("/v1/pair/start", json={"invite": inv["invite_id"]}).json()
            k = pairing.derive_key(inv["secret"], s["pair_id"], s["salt"], s["spki_pin"], s["iterations"])
            gt = c.post("/v1/pair/claim", json={"pair_id": s["pair_id"], "device_name": "gf", "client_proof": pairing.client_proof(k, s["pair_id"], "gf")}).json()["token"]
        owner = httpx.Client(base_url=f"https://127.0.0.1:{api}", verify=False, timeout=60, headers={"Authorization": "Bearer " + ot})
        guest = httpx.Client(base_url=f"https://127.0.0.1:{pub}", verify=False, timeout=60, headers={"Authorization": "Bearer " + gt})
        results: list[dict] = []

        def run(kind: str) -> dict:
            peak = [0]
            stop = [False]

            def sampler():
                while not stop[0]:
                    try:
                        m = adm.get("/admin/metrics").json()
                        for src in m.get("sources", []):
                            peak[0] = max(peak[0], src.get("active_workers", 0) or 0)
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.5)
            t0 = time.time()
            if kind == "owner":
                r = owner.post("/v1/previews", json={"source": {"url": URL}, "hints": {"imdb_id": "tt1254207"}, "layout": "hybrid", "at_ms": 0, "playhead_ms": 0})
                poll = lambda pid: owner.get(f"/v1/previews/{pid}").json()           # noqa: E731
            else:
                r = guest.post("/v1/contribute", json={"source": {"url": URL}, "imdb_id": "tt1254207", "season": 0, "episode": 0, "duration_ms": 596000})
                poll = lambda pid: guest.get(f"/v1/contribute/{pid}").json()          # noqa: E731
            assert r.status_code in (200, 202), (kind, r.status_code, r.text)
            pid = r.json()["preview_id"]
            first = None
            threading.Thread(target=sampler, daemon=True).start()
            while True:
                b = poll(pid)
                if "progress" not in b:
                    raise SystemExit("request failed: " + json.dumps(b)[:200])
                if first is None and b["progress"]["tiles_done"] > 0:
                    first = time.time() - t0
                if b["state"] == "complete" or b["state"] == "failed" or time.time() - t0 > 900:
                    break
                time.sleep(1.5)                                  # the real client's polling cadence
            stop[0] = True
            wall = time.time() - t0
            m = adm.get("/admin/metrics").json()
            src = max(m["sources"], key=lambda x: x.get("done", 0))
            ev = src.get("events", [])
            row = {"kind": kind, "state": b["state"], "tiles": b["progress"]["tiles_done"], "wall_s": round(wall, 1), "first_tile_s": round(first or 0, 1),
                   "tiles_per_min": round(b["progress"]["tiles_done"] / wall * 60, 1), "MB": round(sum(e["bytes"] for e in ev) / 1048576, 1),
                   "requests": sum(e["requests"] for e in ev), "peak_workers_seen": peak[0], "contributed": src.get("contributed")}
            fp = [e for e in adm.get("/admin/status").json()["devices"]]
            # fair next run: delete the entry so the next extraction is cold
            import sqlite3
            con = sqlite3.connect(f"file:{(data / 'openseek.db').as_posix()}?mode=ro", uri=True)
            fps = [r[0] for r in con.execute("select fp from entries")]
            con.close()
            for f in fps:
                adm.delete(f"/admin/library/entries/{f}")
            time.sleep(2)
            return row
        for i in range(ROUNDS):
            for kind in (("owner", "contributor") if i % 2 == 0 else ("contributor", "owner")):
                row = run(kind)
                results.append(row)
                print(json.dumps(row), flush=True)
        for kind in ("owner", "contributor"):
            rows = [r for r in results if r["kind"] == kind and r["state"] == "complete"]
            if rows:
                print(f"{kind:12} median wall {statistics.median(r['wall_s'] for r in rows):6.1f} s   median {statistics.median(r['tiles_per_min'] for r in rows):6.1f} thumbs/min   "
                      f"median {statistics.median(r['MB'] for r in rows):6.1f} MB   requests {statistics.median(r['requests'] for r in rows):.0f}")
        return 0
    finally:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    sys.exit(main())
