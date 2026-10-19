"""Eight real keyframe windows: ONE multipart request vs EIGHT single-range requests, same CDN file, same byte ranges.

Offsets come from the MKV Cues of the exact resolved file (size is asserted), windows are block-aligned like the
extractor's own first read. Each arm gets its own connection; single-range reads reuse one keep-alive connection. Every
request is paced by the production TokenBucket (default 3 RPS, burst 1); pacing wait and transfer time are reported
separately. Strict per-arm read cap, no retries, stop on any unexpected response. Measurement tool only: it does not
decode anything and is not the production extractor.

  python tools/multirange_compare.py --link-file F --expect-size N --out report.json [--rps 3]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from multirange_probe import parse_multipart  # noqa: E402

BLOCK = 256 * 1024
WINDOW = 4 * BLOCK                      # 1 MiB: the extractor's first read on high-bitrate files
CLUSTER_ID = b"\x1f\x43\xb6\x75"
CHUNK = 64 * 1024


class Unexpected(Exception):
    pass


def windows_from_index(link: str, policy, expect_size: int | None, first_slot: int, n: int, step_ms: int = 10_000):
    """Block-aligned windows for n consecutive 10 s slots, from the Cues of the file behind `link`."""
    from openseek.engine import Extractor, ProxyReader
    from openseek.fetch import Budget, ProxySource, RangeProxy, Upstream
    up = Upstream(policy, link, {}, Budget(16 * 1024 * 1024))
    proxy = RangeProxy().start()
    try:
        pre = up.preflight()
        if expect_size is not None and pre.size != expect_size:
            raise Unexpected(f"file size {pre.size} != expected {expect_size}: offsets must not be reused across releases")
        cap = proxy.register(ProxySource(up, pre.size, [(0, pre.head), (pre.tail_offset, pre.tail)]))
        ex = Extractor(ProxyReader(proxy.url(cap), pre.size), pre.fmt)
        try:
            ex.keyframe_times()                                          # loads the Cues
            out = []
            for k in range(first_slot, first_slot + n):
                t_ms = k * step_ms
                target = int((t_ms / 1000.0 + ex.offset_s) / ex.tb)
                e = ex._lookup_index_entry(target)
                if e is None:
                    raise Unexpected("no index entry for slot %d" % k)
                start = (e.pos // BLOCK) * BLOCK
                out.append({"slot": k, "kf_pts_ms": ex._pts_to_ms(e.timestamp), "kf_pos": e.pos, "start": start,
                            "end": min(pre.size, start + WINDOW) - 1})
            stats = {"size": pre.size, "duration_ms": ex.duration_ms, "requests": up.stats.requests, "bytes": up.budget.used}
        finally:
            ex.close()
    finally:
        proxy.stop()
        up.close()
    return out, stats


def _send(conn, target, rng: str) -> tuple[object, float]:
    t0 = time.perf_counter()
    conn.putrequest("GET", target.path, skip_accept_encoding=True)
    conn.putheader("Range", "bytes=" + rng)
    conn.putheader("Accept-Encoding", "identity")
    conn.putheader("User-Agent", "OpenSeek-multirange-compare")
    conn.endheaders()
    resp = conn.getresponse()
    return resp, time.perf_counter() - t0


def _check_status(resp) -> None:
    if resp.status in (429, 503) or resp.getheader("Retry-After"):
        raise Unexpected(f"rate limited: HTTP {resp.status}")
    if resp.status != 206:
        raise Unexpected(f"HTTP {resp.status}")


def arm_single(policy, target, wins: list[dict], bucket, cap: int) -> dict:
    t_conn0 = time.perf_counter()
    conn = policy.connect(target, 15.0)
    connect_s = time.perf_counter() - t_conn0
    rows, data, got_total = [], [], 0
    wall0 = time.perf_counter()
    try:
        for w in wins:
            t = time.perf_counter()
            bucket.throttle(1, lambda: True)
            pace = time.perf_counter() - t
            resp, ttfb = _send(conn, target, f"{w['start']}-{w['end']}")
            _check_status(resp)
            want = w["end"] - w["start"] + 1
            tb = time.perf_counter()
            body = bytearray()
            while len(body) < want:
                if got_total + len(body) > cap:
                    raise Unexpected("read cap reached")
                chunk = resp.read(min(CHUNK, want - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            body_s = time.perf_counter() - tb
            got_total += len(body)
            cr = resp.getheader("Content-Range")
            rows.append({"slot": w["slot"], "pace_wait_s": round(pace, 3), "ttfb_ms": round(ttfb * 1000), "body_ms": round(body_s * 1000),
                         "bytes": len(body), "content_range": cr,
                         "ok": cr == f"bytes {w['start']}-{w['end']}/" + (cr.split("/")[-1] if cr else "") and len(body) == want})
            data.append(bytes(body))
    finally:
        try:
            conn.sock and conn.sock.close()
        except OSError:
            pass
    wall = time.perf_counter() - wall0
    transfer = sum(r["ttfb_ms"] + r["body_ms"] for r in rows) / 1000
    return {"requests": len(rows), "connect_ms": round(connect_s * 1000), "wall_s_paced": round(wall, 3),
            "pace_wait_s": round(sum(r["pace_wait_s"] for r in rows), 3), "transfer_s": round(transfer, 3), "bytes": got_total,
            "MBps_wall": round(got_total / wall / 1e6, 2), "MBps_transfer": round(got_total / transfer / 1e6, 2),
            "per_request": rows, "_data": data}


def arm_multi(policy, target, wins: list[dict], bucket, cap: int) -> dict:
    t_conn0 = time.perf_counter()
    conn = policy.connect(target, 15.0)
    connect_s = time.perf_counter() - t_conn0
    wall0 = time.perf_counter()
    try:
        bucket.throttle(1, lambda: True)
        pace = time.perf_counter() - wall0
        resp, ttfb = _send(conn, target, ",".join(f"{w['start']}-{w['end']}" for w in wins))
        _check_status(resp)
        ctype = resp.getheader("Content-Type") or ""
        if not ctype.lower().startswith("multipart/byteranges"):
            raise Unexpected(f"content-type {ctype!r}")
        boundary = ctype.split("boundary=")[1].strip('"; ')
        samples, body = [], bytearray()
        t_body = time.perf_counter()
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            body.extend(chunk)
            samples.append((time.perf_counter() - wall0, len(body)))
            if len(body) > cap:
                raise Unexpected("read cap reached")
        body_s = time.perf_counter() - t_body
    finally:
        try:
            conn.sock and conn.sock.close()
        except OSError:
            pass
    wall = time.perf_counter() - wall0
    parts = parse_multipart(bytes(body), boundary)
    # part i is complete once the bytes up to the next boundary have arrived
    delim = b"--" + boundary.encode()
    ends, pos = [], 0
    for _ in range(len(wins)):
        pos = body.find(delim, pos + 1)
        ends.append(pos)
    ends = ends[1:] + [len(body)]
    done_at = [next((t for t, n in samples if n >= e), wall) for e in ends]
    # recover each part's payload for the byte comparison
    segs = bytes(body).split(delim)[1:]
    payloads = []
    for seg in segs:
        if seg.startswith(b"--"):
            break
        _, _, d = seg.lstrip(b"\r\n").partition(b"\r\n\r\n")
        payloads.append(d[:-2] if d.endswith(b"\r\n") else d)
    ok = len(parts) == len(wins) and all(p.get("range") == (w["start"], w["end"]) and p.get("len") == w["end"] - w["start"] + 1
                                         for p, w in zip(parts, wins))
    return {"requests": 1, "connect_ms": round(connect_s * 1000), "wall_s_paced": round(wall, 3), "pace_wait_s": round(pace, 3),
            "ttfb_ms": round(ttfb * 1000), "body_ms": round(body_s * 1000), "transfer_s": round(ttfb + body_s, 3), "bytes": len(body),
            "MBps_wall": round(len(body) / wall / 1e6, 2), "MBps_transfer": round(len(body) / (ttfb + body_s) / 1e6, 2),
            "parts": len(parts), "parts_ok": ok, "content_type": ctype.split(";")[0],
            "part_complete_s": [round(t, 3) for t in done_at],
            "progressive": bool(done_at) and done_at[0] < 0.6 * done_at[-1], "recv_chunks": len(samples),
            "multipart_overhead_bytes": len(body) - sum(len(p) for p in payloads), "_data": payloads}


def run(link: str, policy, expect_size: int | None, rps: float, first_slot: int = 300, n: int = 8, cap_mib: int = 12) -> dict:
    from openseek.fetch import TokenBucket
    wins, idx = windows_from_index(link, policy, expect_size, first_slot, n)
    target = policy.vet(link)
    cap = cap_mib * 1024 * 1024
    rep: dict = {"index": idx, "windows": [{k: w[k] for k in ("slot", "kf_pts_ms", "kf_pos", "start", "end")} for w in wins],
                 "rps": rps, "order": "multi first (cold), single second (warm): any caching bias favours single-range"}
    bucket = TokenBucket(rps, burst=1)
    bucket.throttle(1, lambda: True)                              # start with an empty bucket: first real request waits too
    try:
        multi = arm_multi(policy, target, wins, bucket, cap)
        single = arm_single(policy, target, wins, bucket, cap)
    except Unexpected as e:
        rep["stopped"] = str(e)
        return rep
    mdata, sdata = multi.pop("_data"), single.pop("_data")
    same = [m == s for m, s in zip(mdata, sdata)]
    # does the index position really sit at a Matroska Cluster inside the bytes we got? (offsets belong to this file)
    at_cluster = [d[w["kf_pos"] - w["start"]:w["kf_pos"] - w["start"] + 4] == CLUSTER_ID for d, w in zip(sdata, wins)]
    has_cluster = [CLUSTER_ID in d for d in sdata]
    rep.update(multi=multi, single=single, bytes_identical_per_window=same, kf_pos_is_cluster_start=at_cluster,
               window_contains_cluster_id=has_cluster)
    rep["total_bytes_both_arms"] = multi["bytes"] + single["bytes"]
    rep["speedup_wall_paced"] = round(single["wall_s_paced"] / multi["wall_s_paced"], 2)
    rep["speedup_transfer_only"] = round(single["transfer_s"] / multi["transfer_s"], 2)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link-file", required=True)
    ap.add_argument("--expect-size", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rps", type=float, default=3.0)
    ap.add_argument("--first-slot", type=int, default=300)
    a = ap.parse_args()
    from openseek.config import Settings
    from openseek.netpolicy import DestinationPolicy
    s = Settings()
    link = Path(a.link_file).read_text().strip()
    try:
        rep = run(link, DestinationPolicy(s.allowed_ports, s.allow_http_sources), a.expect_size, a.rps, a.first_slot)
    except Exception as e:  # noqa: BLE001 - no retries: report and stop
        rep = {"stopped": f"{type(e).__name__}: {str(e)[:100]}"}
    Path(a.out).write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return 0 if "stopped" not in rep else 2


if __name__ == "__main__":
    sys.exit(main())
