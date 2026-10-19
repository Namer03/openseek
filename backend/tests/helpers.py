"""Shared test helpers: fixture generation (ffmpeg), frame-number barcode, OSP1 client side,
live uvicorn server with TLS."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from openseek.app import Core, create_admin_app, create_api_app, public_config, uvicorn_configs  # noqa: E402
from openseek.config import Settings  # noqa: E402
from openseek.netpolicy import DestinationPolicy, TestOnlyAllowance  # noqa: E402
from openseek.pairing import client_proof, derive_key, server_proof  # noqa: E402

# Synthetic media for the tests that need real files; generate with fixtures/gen_altsrc.py (see fixtures/README.md).
FIXTURES = Path(os.environ.get("OPENSEEK_TEST_FIXTURES") or Path(__file__).resolve().parent / "fixtures" / "generated")
FIXTURES_HINT = ("synthetic fixtures not generated in %s; run `python backend/tests/fixtures/gen_altsrc.py` "
                 "(needs ffmpeg) or set OPENSEEK_TEST_FIXTURES" % FIXTURES)

FPS = 24
BITS = 18
GEQ_LUM = ("if(eq(Y\\,0)\\,1\\,0)*255*mod(floor(N/pow(2\\,X))\\,2)"
           "+if(eq(Y\\,1)\\,1\\,0)*255*(1-mod(floor(N/pow(2\\,X))\\,2))")


# ----------------------------------------------------------------------------- fixtures
def _barcode_source(w: int, h: int) -> str:
    return f"color=black:s={BITS}x2:r={FPS},format=gray,geq=lum='{GEQ_LUM}',scale={w}:{h // 9}:flags=neighbor"


def make_video(out: Path, seconds: int, w: int = 640, h: int = 360, gop: int = 48, kbps: int = 400,
               extra: list[str] | None = None, hevc_pq: bool = False) -> Path:
    graph = "[0:v][1:v]overlay=0:0:shortest=1,format=yuv420p[v]"
    if hevc_pq:
        graph = ("[0:v][1:v]overlay=0:0:shortest=1,format=yuv420p10le,"
                 "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc[v]")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={FPS}",
            "-f", "lavfi", "-i", _barcode_source(w, h),
            "-filter_complex", graph, "-map", "[v]", "-frames:v", str(seconds * FPS)]
    if hevc_pq:
        args += ["-c:v", "libx265", "-preset", "ultrafast", "-b:v", f"{kbps}k", "-pix_fmt", "yuv420p10le",
                 "-x265-params", f"keyint={gop}:min-keyint={gop}:scenecut=0:log-level=error:"
                                 "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                 "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
    else:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", f"{kbps}k", "-g", str(gop),
                 "-keyint_min", str(gop), "-sc_threshold", "0",
                 "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    args += (extra or []) + [str(out)]
    subprocess.run(args, check=True)
    return out


def remux(src: Path, out: Path, extra: list[str] | None = None) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0", "-c", "copy",
                    *(extra or []), str(out)], check=True)
    return out


def _read_vint(f, keep_marker: bool):
    first = f.read(1)
    if not first:
        raise EOFError
    b0 = first[0]
    length, mask = 1, 0x80
    while length <= 8 and not (b0 & mask):
        mask >>= 1
        length += 1
    val = b0 if keep_marker else (b0 & (mask - 1))
    for c in f.read(length - 1):
        val = (val << 8) | c
    return val, length


def void_out_cues(path: Path) -> int:
    """Overwrite top-level Cues with an EBML Void element in place (same method as bench/fixtures.py)."""
    n = 0
    with open(path, "r+b") as f:
        _read_vint(f, True)
        size, _ = _read_vint(f, False)
        f.seek(size, 1)
        eid, _ = _read_vint(f, True)
        seg_size, sl = _read_vint(f, False)
        seg_start = f.tell()
        unknown = seg_size == (1 << (7 * sl)) - 1
        end = os.path.getsize(path) if unknown else seg_start + seg_size
        while f.tell() < end:
            pos = f.tell()
            try:
                eid, _ = _read_vint(f, True)
                size, _ = _read_vint(f, False)
            except EOFError:
                break
            body = f.tell()
            if eid == 0x1C53BB6B:
                total = (body - pos) + size
                f.seek(pos)
                f.write(bytes([0xEC]) + bytes([0x01]) + struct.pack(">Q", total - 9)[1:])
                n += 1
            f.seek(body + size)
    return n


def decode_barcode(jpeg: bytes, box=(0, 0, 320, 180)) -> int | None:
    """Frame number burned into the fixture, read from a thumbnail (None if undecodable)."""
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as im:
        g = im.convert("L")
        px = g.load()
        x0, y0, w, h = box
        n = 0
        for i in range(BITS):
            cx = x0 + (i + 0.5) * w / BITS
            vals = []
            for row in (0, 1):
                cy = y0 + (row * 2 + 1) * h / 36.0
                acc = cnt = 0
                for dx in (-2, 0, 2):
                    for dy in (-1, 0, 1):
                        acc += px[min(max(int(cx + dx), 0), g.width - 1), min(max(int(cy + dy), 0), g.height - 1)]
                        cnt += 1
                vals.append(acc / cnt)
            a, b = vals
            if abs(a - b) < 50:
                return None
            if a > b:
                n |= 1 << i
        return n


# ----------------------------------------------------------------------------- policy
def make_policy(origins, names: dict[str, list[str]] | None = None, **kw) -> DestinationPolicy:
    """Policy for tests: injected resolver + exact (127.0.0.1, port) allowance for fixture servers."""
    names = dict(names or {})

    def resolver(host: str, port: int) -> list[str]:
        if host in names:
            return names[host]
        raise socket.gaierror("unknown test host")

    allowance = TestOnlyAllowance(frozenset(("127.0.0.1", o.port) for o in origins))
    return DestinationPolicy(resolver=resolver, allowance=allowance, **kw)


# ----------------------------------------------------------------------------- live server
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def peer_pin(host: str, port: int) -> str:
    from openseek.tls import spki_pin_from_cert_der

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname="openseek") as s:
            return spki_pin_from_cert_der(s.getpeercert(True))


class LiveServer:
    """Real uvicorn (TLS API + loopback admin) in a background thread, sharing one Core."""

    def __init__(self, data_dir: Path, policy: DestinationPolicy, **overrides):
        import uvicorn

        self.port, self.admin_port = free_port(), free_port()
        if overrides.get("public_port") == -1:            # tests ask for an ephemeral public listener
            overrides["public_port"] = free_port()
        base = dict(data_dir=Path(data_dir), host="127.0.0.1", port=self.port, admin_port=self.admin_port,
                    create_wait_s=3.0, frame_timeout_s=20.0)
        base.update(overrides)
        self.settings = Settings().with_(**base)
        self.core = Core(self.settings, policy).start()
        api_cfg, admin_cfg = uvicorn_configs(self.core, create_api_app(self.core), create_admin_app(self.core))
        self.servers = [uvicorn.Server(api_cfg), uvicorn.Server(admin_cfg)]
        self.public_base = None
        pub = public_config(self.core, create_api_app(self.core, public=True))
        if pub is not None:
            self.servers.append(uvicorn.Server(pub))
            self.public_port = self.settings.public_port
            self.public_base = f"https://127.0.0.1:{self.public_port}"
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        while not all(s.started for s in self.servers):
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        self.base = f"https://127.0.0.1:{self.port}"
        self.admin = f"http://127.0.0.1:{self.admin_port}"

    async def _main(self):
        await asyncio.gather(*(s.serve() for s in self.servers))

    def stop(self) -> None:
        for s in self.servers:
            s.should_exit = True
        self.thread.join(timeout=15)
        self.core.stop()

    def client(self, token: str | None = None, timeout: float = 30.0):
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # the pin is verified separately (peer_pin); httpx is used for the request plumbing
        return httpx.Client(base_url=self.base, verify=False, headers=headers, timeout=timeout)

    def public_client(self, token: str | None = None, timeout: float = 30.0):
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(base_url=self.public_base, verify=False, headers=headers, timeout=timeout)

    def admin_client(self):
        import httpx

        return httpx.Client(base_url=self.admin, headers={"X-OpenSeek-Admin": "1"}, timeout=30.0)

    def pair(self, name: str = "Shield") -> tuple[str, str]:
        """Full OSP1 pairing: admin opens a window, client derives K over the PINNED key."""
        with self.admin_client() as a:
            code = a.post("/admin/pair").json()["code"]
        pin = peer_pin("127.0.0.1", self.port)
        with self.client() as c:
            st = c.post("/v1/pair/start", json={}).json()
            assert st["spki_pin"] == pin
            k = derive_key(code, st["pair_id"], st["salt"], st["spki_pin"], st["iterations"])
            r = c.post("/v1/pair/claim", json={"pair_id": st["pair_id"], "device_name": name,
                                               "client_proof": client_proof(k, st["pair_id"], name)})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["server_proof"] == server_proof(k, st["pair_id"], body["token"])
            return body["token"], body["device_id"]


def wait_for(pred, timeout: float = 30.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("condition not met within %.1f s" % timeout)


def parse_vtt(text: str) -> list[tuple[int, int, str]]:
    """[(start_ms, end_ms, url)] with strict WebVTT timestamp parsing."""
    lines = text.split("\n")
    assert lines[0] == "WEBVTT"

    def ms(t: str) -> int:
        h, m, rest = t.split(":")
        s, f = rest.split(".")
        assert len(h) >= 2 and len(m) == 2 and len(s) == 2 and len(f) == 3
        assert int(m) < 60 and int(s) < 60
        return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(f)

    out = []
    i = 1
    while i < len(lines):
        if "-->" in lines[i]:
            a, b = (x.strip() for x in lines[i].split("-->"))
            out.append((ms(a), ms(b), lines[i + 1]))
            i += 2
        else:
            i += 1
    return out


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sha256hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
