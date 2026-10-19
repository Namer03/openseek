"""ONE HTTP multi-range compatibility probe (protocol support only; says nothing about speed or safety under load).

Sends a single GET carrying eight small non-overlapping ranges (4 KiB each, 32 KiB total) over the vetted connection path
(SSRF policy, pinned address), reads at most CAP bytes of the answer, then closes. No retries, no redirects. The link is
read from a file and never printed. Verdicts:
  SUCCESS            206 + multipart/byteranges, 8 parts, every Content-Range and length correct, same total size
  SINGLE_RANGE_ONLY  206 with one Content-Range: the extra ranges were ignored
  IGNORED_RANGE      200: the server would stream the whole file (we stop at CAP)
  UNEXPECTED         anything else (status, content type, count, offsets, sizes)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PART = 4096
CAP = 128 * 1024                         # hard ceiling on bytes read from the answer
GiB = 1 << 30


def default_ranges(n: int = 8) -> list[tuple[int, int]]:
    """Non-overlapping, well inside any 4K remux (>= 60 GiB): one range every 8 GiB, 1 MiB in."""
    return [(k * 8 * GiB + (1 << 20), k * 8 * GiB + (1 << 20) + PART - 1) for k in range(n)]


def parse_multipart(body: bytes, boundary: str) -> list[dict]:
    """Parts of a multipart/byteranges body: [{'range': (a, b), 'total': n, 'len': n, 'sha256': ...}]."""
    parts = []
    delim = b"--" + boundary.encode()
    chunks = body.split(delim)
    for ch in chunks[1:]:
        if ch.startswith(b"--"):
            break
        head, sep, data = ch.lstrip(b"\r\n").partition(b"\r\n\r\n")
        if not sep:
            parts.append({"error": "malformed part"})
            continue
        m = re.search(rb"(?i)content-range:\s*bytes\s+(\d+)-(\d+)/(\d+|\*)", head)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if not m:
            parts.append({"error": "no content-range", "len": len(data)})
            continue
        parts.append({"range": (int(m.group(1)), int(m.group(2))), "total": None if m.group(3) == b"*" else int(m.group(3)),
                      "len": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16]})
    return parts


def verdict(status: int, headers: dict, body: bytes, truncated: bool, ranges: list[tuple[int, int]]) -> tuple[str, str]:
    ctype = headers.get("content-type", "")
    if status == 200:
        return "IGNORED_RANGE", f"HTTP 200 (whole-file answer); stopped after {len(body)} bytes"
    if status != 206:
        return "UNEXPECTED", f"HTTP {status}"
    if not ctype.lower().startswith("multipart/byteranges"):
        cr = headers.get("content-range", "")
        if cr:
            return "SINGLE_RANGE_ONLY", f"HTTP 206 with Content-Range {cr}; content-type {ctype!r}"
        return "UNEXPECTED", f"HTTP 206 but content-type {ctype!r}"
    m = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
    if not m:
        return "UNEXPECTED", "multipart without a boundary"
    if truncated:
        return "UNEXPECTED", "response larger than the strict cap"
    parts = parse_multipart(body, m.group(1))
    if len(parts) != len(ranges):
        return "UNEXPECTED", f"{len(parts)} parts, expected {len(ranges)}"
    totals = {p.get("total") for p in parts}
    for want, p in zip(ranges, parts):
        if "error" in p:
            return "UNEXPECTED", p["error"]
        if p["range"] != want:
            return "UNEXPECTED", f"Content-Range {p['range']} != requested {want}"
        if p["len"] != want[1] - want[0] + 1:
            return "UNEXPECTED", f"part {want} returned {p['len']} bytes"
    if len(totals) != 1 or None in totals:
        return "UNEXPECTED", f"inconsistent total sizes {sorted(map(str, totals))}"
    return "SUCCESS", f"{len(parts)} parts, {sum(p['len'] for p in parts)} bytes, total size {totals.pop()}"


def probe(url: str, policy, ranges: list[tuple[int, int]], timeout: float = 15.0, cap: int = CAP) -> dict:
    target = policy.vet(url)
    conn = policy.connect(target, timeout)
    t0 = time.perf_counter()
    rep: dict = {"ranges": len(ranges), "requested_bytes": sum(b - a + 1 for a, b in ranges), "requests_sent": 1}
    try:
        conn.putrequest("GET", target.path, skip_accept_encoding=True)
        conn.putheader("Range", "bytes=" + ",".join(f"{a}-{b}" for a, b in ranges))
        conn.putheader("Accept-Encoding", "identity")
        conn.putheader("User-Agent", "OpenSeek-multirange-probe")
        conn.endheaders()
        resp = conn.getresponse()
        rep["ttfb_ms"] = round((time.perf_counter() - t0) * 1000)
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = bytearray()
        truncated = False
        while len(body) < cap + 1:
            chunk = resp.read(min(8192, cap + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > cap:
            truncated, body = True, body[:cap]
        rep.update(status=resp.status, content_type=headers.get("content-type"), content_length=headers.get("content-length"),
                   accept_ranges=headers.get("accept-ranges"), server=headers.get("server"), via=headers.get("via"),
                   body_bytes_read=len(body), truncated_at_cap=truncated, total_ms=round((time.perf_counter() - t0) * 1000))
        if resp.status in (429, 503) or headers.get("retry-after"):
            rep["verdict"], rep["detail"] = "UNEXPECTED", f"HTTP {resp.status}, Retry-After {headers.get('retry-after')}"
        else:
            rep["verdict"], rep["detail"] = verdict(resp.status, headers, bytes(body), truncated, ranges)
    finally:
        try:
            conn.sock and conn.sock.close()                          # never keep or drain the connection
        except OSError:
            pass
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link-file", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from openseek.config import Settings
    from openseek.netpolicy import DestinationPolicy
    s = Settings()
    link = Path(a.link_file).read_text().strip()
    try:
        rep = probe(link, DestinationPolicy(s.allowed_ports, s.allow_http_sources), default_ranges())
    except Exception as e:  # noqa: BLE001 - one request, no retry: report and stop
        rep = {"verdict": "UNEXPECTED", "detail": f"{type(e).__name__}: {str(e)[:80]}", "requests_sent": 1}
    Path(a.out).write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return 0 if rep["verdict"] == "SUCCESS" else 2


if __name__ == "__main__":
    sys.exit(main())
