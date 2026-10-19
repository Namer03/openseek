"""OpenSeek extraction prototype: seek-preview frames from a remote HTTP video.

All subprocesses use argument arrays (never shell=True). URLs are never printed; any
ffmpeg stderr is passed through redact.redact_text before it is stored or shown.

Strategies
  ffmpeg_frame(...)        S1/S2: one ffmpeg process per frame (input seeking), options for
                           -noaccurate_seek / -skip_frame nokey / bounded -request_size
  ffmpeg_multi(...)        S3: one ffmpeg process, one "-ss T -i URL" input per timestamp
  ffmpeg_sequential(...)   S4: naive full decode with fps filter
  AvExtractor              S5: PyAV in-process, container opened once, seek() per timestamp;
                           io="libav" (libav's http) or io="ranged" (RangeReader below:
                           bounded Range requests over one keep-alive connection + block cache)

Thumbnail geometry: scale to fit inside 320x180 preserving aspect ratio (bilinear), then pad
(letterbox/pillarbox, black) to exactly 320x180, SAR 1, JPEG (-q:v 5 / Pillow quality 80).
HDR (PQ / HLG) sources are tonemapped to BT.709 SDR: downscale first in the source format, then
zscale linearize -> tonemap (hable) -> BT.709. Doing the tonemap at 320x180 instead of 4K makes it
nearly free (measured in REPORT.md).
"""
from __future__ import annotations

import argparse
import ctypes
import http.client
import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import psutil

from redact import redact_text, redact_url

FFMPEG = os.environ.get("OPENSEEK_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("OPENSEEK_FFPROBE", "ffprobe")
THUMB_W, THUMB_H = 320, 180
UA = "OpenSeek-bench/0.1"

SDR_VF = (f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease:flags=bilinear,"
          f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuvj420p")
# tonemap after downscale (cheap) -- the scale keeps the 10-bit format
HDR_VF_SMALL = (f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease:flags=bilinear,"
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,"
                f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuvj420p")
# tonemap at full resolution (the common copy-pasted recipe) -- for cost comparison only
HDR_VF_FULL = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
               "zscale=t=bt709:m=bt709:r=tv,format=yuv420p," + SDR_VF)
HDR_VF_PLACEBO = (f"libplacebo=w={THUMB_W}:h={THUMB_H}:force_original_aspect_ratio=decrease:"
                  "colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv:"
                  "tonemapping=bt.2390:format=yuv420p,"
                  f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuvj420p")

HDR_TRC = {"smpte2084", "arib-std-b67"}


# ---------------------------------------------------------------- process accounting
@dataclass
class ProcStats:
    wall_s: float = 0.0
    cpu_s: float | None = None
    peak_rss: int | None = None
    returncode: int | None = None
    killed: str | None = None       # "timeout" | "cancelled" | None
    orphan: bool = False             # True if the process was still alive after kill+wait
    stderr: str = ""                 # redacted tail


if os.name == "nt":
    from ctypes import wintypes

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]

    def _final_usage(proc: subprocess.Popen) -> tuple[float | None, int | None]:
        """Exact user+sys CPU and peak working set of an exited process (handle still open)."""
        h = wintypes.HANDLE(int(proc._handle))  # type: ignore[attr-defined]
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        cpu = None
        if _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            tok = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
            cpu = tok(k) + tok(u)
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        rss = pmc.PeakWorkingSetSize if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else None
        return cpu, rss
else:
    def _final_usage(proc):  # POSIX: sampled values are used instead
        return None, None


def run_proc(args: list[str], timeout_s: float, cancel: threading.Event | None = None,
             env: dict | None = None, stdout_sink: list | None = None) -> ProcStats:
    """Run a command (argument array, no shell) with a hard wall-clock limit and cancellation.
    If stdout_sink is a list, the decoded stdout is appended to it."""
    assert isinstance(args, list) and all(isinstance(a, str) for a in args)
    st = ProcStats()
    t0 = time.perf_counter()
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if stdout_sink is not None else subprocess.DEVNULL,
                            stderr=subprocess.PIPE, shell=False, env=env)
    err_chunks: list[bytes] = []
    out_chunks: list[bytes] = []
    rd = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    rd.start()
    if stdout_sink is not None:
        ro = threading.Thread(target=lambda: out_chunks.append(proc.stdout.read()), daemon=True)
        ro.start()
    ps = None
    try:
        ps = psutil.Process(proc.pid)
    except psutil.Error:
        pass
    peak = 0
    cpu_sample = 0.0
    while True:
        if proc.poll() is not None:
            break
        if ps is not None and os.name != "nt":
            try:
                peak = max(peak, ps.memory_info().rss)
                t = ps.cpu_times()
                cpu_sample = t.user + t.system
            except psutil.Error:
                pass
        if cancel is not None and cancel.is_set():
            st.killed = "cancelled"
        elif time.perf_counter() - t0 > timeout_s:
            st.killed = "timeout"
        if st.killed:
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
            break
        time.sleep(0.005)
    st.wall_s = time.perf_counter() - t0
    st.returncode = proc.poll()
    st.orphan = st.returncode is None or (ps is not None and ps.is_running() and
                                          ps.status() != psutil.STATUS_ZOMBIE)
    cpu, rss = _final_usage(proc)
    st.cpu_s = cpu if cpu is not None else (cpu_sample or None)
    st.peak_rss = rss if rss is not None else (peak or None)
    rd.join(2)
    if stdout_sink is not None:
        ro.join(2)
        stdout_sink.append(b"".join(out_chunks).decode("utf-8", "replace"))
    raw = b"".join(err_chunks).decode("utf-8", "replace")
    st.stderr = redact_text(raw)[-1500:]
    return st


def classify(st: ProcStats, produced: bool) -> str | None:
    """Return None on success or a stable error category."""
    if st.killed == "timeout":
        return "timeout"
    if st.killed == "cancelled":
        return "cancelled"
    e = st.stderr.lower()
    if "403" in e or "forbidden" in e:
        return "http_403_expired"
    if "404" in e or "not found" in e:
        return "http_404"
    if "401" in e:
        return "http_401"
    if "timed out" in e or "timeout" in e or "error number -138" in e or "error number -110" in e:
        return "timeout"  # -138 = ETIMEDOUT on Windows (MSVC errno), -110 on Linux
    if "invalid data found" in e or "could not find codec" in e or "unsupported" in e \
            or "no such filter" in e or "decoder" in e and "not found" in e:
        return "unsupported"
    if "connection refused" in e or "error number -138" in e or "connection reset" in e \
            or "end of file" in e and st.returncode:
        return "network"
    if st.returncode != 0:
        return "ffmpeg_error"
    if not produced:
        return "no_frame"
    return None


# ---------------------------------------------------------------- options
@dataclass
class HttpOpts:
    rw_timeout_s: float = 10.0
    user_agent: str = UA
    multiple_requests: bool = False
    reconnect: bool = True
    seekable: int | None = None        # None = auto
    request_size: int = 0              # ffmpeg>=8 bounded Range requests (0 = open-ended)
    initial_request_size: int = 0
    probesize: int = 2_000_000
    analyzeduration_us: int = 2_000_000

    def args(self, is_http: bool) -> list[str]:
        a = ["-rw_timeout", str(int(self.rw_timeout_s * 1e6))]
        if is_http:
            a += ["-user_agent", self.user_agent]
            if self.multiple_requests:
                a += ["-multiple_requests", "1"]
            if self.reconnect:
                a += ["-reconnect", "1", "-reconnect_on_network_error", "1",
                      "-reconnect_delay_max", "2", "-reconnect_max_retries", "2"]
            if self.seekable is not None:
                a += ["-seekable", str(self.seekable)]
            if self.request_size:
                a += ["-request_size", str(self.request_size)]
            if self.initial_request_size:
                a += ["-initial_request_size", str(self.initial_request_size)]
        a += ["-probesize", str(self.probesize), "-analyzeduration", str(self.analyzeduration_us)]
        return a


def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _base_cmd() -> list[str]:
    return [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]


# ---------------------------------------------------------------- preflight
def preflight(url: str, timeout_s: float = 10.0, max_redirects: int = 3) -> dict:
    """Cheap source check before any ffmpeg work: one 'Range: bytes=0-0' GET (following
    redirects). A production backend should reject sources that do not answer 206."""
    t0 = time.perf_counter()
    out = {"ok": False, "range": False, "size": None, "status": None, "redirects": 0, "error": None}
    try:
        rr = RangeReader(url, block=4096, timeout_s=timeout_s, max_redirects=max_redirects)
        out.update(ok=True, range=True, size=rr.size, status=206)
        rr.close()
    except HttpError as e:
        out.update(status=e.status, error=e.category)
    except (OSError, http.client.HTTPException) as e:
        out.update(error="network:" + type(e).__name__)
    out["s"] = round(time.perf_counter() - t0, 4)
    return out


# ---------------------------------------------------------------- probe
@dataclass
class ProbeResult:
    ok: bool
    error: str | None = None
    duration: float | None = None
    vcodec: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    color_transfer: str | None = None
    hdr: bool = False
    proc: ProcStats = field(default_factory=ProcStats)


def probe(url: str, http: HttpOpts | None = None, timeout_s: float = 20.0) -> ProbeResult:
    http = http or HttpOpts()
    args = [FFPROBE, "-hide_banner", "-loglevel", "error", *http.args(_is_http(url)),
            "-select_streams", "v:0", "-show_entries",
            "format=duration:stream=codec_name,width,height,pix_fmt,color_transfer",
            "-of", "json", url]
    # run_proc discards stdout; ffprobe needs it -> small dedicated runner
    t0 = time.perf_counter()
    try:
        cp = subprocess.run(args, capture_output=True, timeout=timeout_s, shell=False,
                            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ProbeResult(False, "timeout", proc=ProcStats(wall_s=time.perf_counter() - t0, killed="timeout"))
    st = ProcStats(wall_s=time.perf_counter() - t0, returncode=cp.returncode,
                   stderr=redact_text(cp.stderr.decode("utf-8", "replace"))[-1500:])
    if cp.returncode != 0:
        return ProbeResult(False, classify(st, False) or "ffprobe_error", proc=st)
    j = json.loads(cp.stdout or b"{}")
    s = (j.get("streams") or [{}])[0]
    trc = s.get("color_transfer")
    dur = j.get("format", {}).get("duration")
    return ProbeResult(True, None, float(dur) if dur else None, s.get("codec_name"), s.get("width"),
                       s.get("height"), s.get("pix_fmt"), trc, trc in HDR_TRC, st)


# ---------------------------------------------------------------- S1 / S2
@dataclass
class FrameResult:
    ok: bool
    error: str | None
    t: float
    out: str | None
    proc: ProcStats


def thumb_vf(hdr: bool, mode: str = "small") -> str:
    if not hdr:
        return SDR_VF
    return {"small": HDR_VF_SMALL, "full": HDR_VF_FULL, "placebo": HDR_VF_PLACEBO}[mode]


def ffmpeg_frame(url: str, t: float, out: str | Path, *, http: HttpOpts | None = None,
                 accurate: bool = True, skip_nokey: bool = False, hdr: bool = False,
                 hdr_mode: str = "small", timeout_s: float = 30.0,
                 cancel: threading.Event | None = None, threads: int = 0,
                 thread_type: str | None = None) -> FrameResult:
    http = http or HttpOpts()
    out = str(out)
    try:
        os.remove(out)
    except FileNotFoundError:
        pass
    args = _base_cmd()
    if hdr and hdr_mode == "placebo":
        args += ["-init_hw_device", "vulkan"]
    args += http.args(_is_http(url))
    if threads:
        args += ["-threads", str(threads)]
    if thread_type:
        args += ["-thread_type", thread_type]  # decoder option; "slice" avoids frame-thread delay
    if skip_nokey:
        args += ["-skip_frame", "nokey"]
    if not accurate:
        args += ["-noaccurate_seek"]
    args += ["-ss", f"{t:.3f}", "-i", url, "-map", "0:v:0", "-an", "-sn", "-dn",
             "-frames:v", "1", "-fps_mode", "passthrough", "-vf", thumb_vf(hdr, hdr_mode), "-q:v", "5",
             "-f", "image2", "-update", "1", out]
    st = run_proc(args, timeout_s, cancel)
    produced = os.path.exists(out) and os.path.getsize(out) > 0
    err = diagnose(url, classify(st, produced))
    return FrameResult(err is None, err, t, out if produced else None, st)


def diagnose(url: str, err: str | None) -> str | None:
    """ffmpeg at -loglevel error often hides the HTTP status of a failed *seek* request (it just
    reports a truncated file / no frame). On an ambiguous failure, one cheap ranged GET tells us
    whether the link expired (403), vanished (404) or lost Range support."""
    if err in ("no_frame", "ffmpeg_error", "network", "unsupported") and _is_http(url):
        pf = preflight(url, timeout_s=5)
        if not pf["ok"] and pf["error"]:
            return pf["error"]
    return err


# ---------------------------------------------------------------- S3
def ffmpeg_multi(url: str, ts: list[float], outdir: str | Path, *, http: HttpOpts | None = None,
                 accurate: bool = True, hdr: bool = False, timeout_s: float = 120.0,
                 cancel: threading.Event | None = None) -> tuple[list[str | None], ProcStats, str | None]:
    """One process, one input per timestamp; each output gets 1 frame."""
    http = http or HttpOpts()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    args = _base_cmd()
    for t in ts:
        args += http.args(_is_http(url))
        if not accurate:
            args += ["-noaccurate_seek"]
        args += ["-ss", f"{t:.3f}", "-i", url]
    outs = []
    for i, _ in enumerate(ts):
        o = outdir / f"m{i:04d}.jpg"
        if o.exists():
            o.unlink()
        outs.append(o)
        args += ["-map", f"{i}:v:0", "-frames:v", "1", "-vf", thumb_vf(hdr), "-q:v", "5",
                 "-f", "image2", "-update", "1", str(o)]
    st = run_proc(args, timeout_s, cancel)
    got = [str(o) if o.exists() and o.stat().st_size else None for o in outs]
    err = classify(st, all(got))
    return got, st, err


# ---------------------------------------------------------------- S4
def ffmpeg_sequential(url: str, interval_s: float, outdir: str | Path, *, http: HttpOpts | None = None,
                      hdr: bool = False, duration_s: float | None = None, timeout_s: float = 3600,
                      cancel: threading.Event | None = None) -> tuple[list[str], ProcStats, str | None]:
    http = http or HttpOpts()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for p in outdir.glob("s*.jpg"):
        p.unlink()
    args = _base_cmd() + http.args(_is_http(url)) + ["-i", url]
    if duration_s:
        args += ["-t", str(duration_s)]
    # fps=1/interval with round=down picks the first frame at/after each multiple of interval
    args += ["-map", "0:v:0", "-an", "-vf", f"fps=1/{interval_s}:round=down," + thumb_vf(hdr),
             "-q:v", "5", "-f", "image2", str(outdir / "s%05d.jpg")]
    st = run_proc(args, timeout_s, cancel)
    got = sorted(str(p) for p in outdir.glob("s*.jpg"))
    return got, st, classify(st, bool(got))


# ---------------------------------------------------------------- S5: PyAV
class HttpError(Exception):
    def __init__(self, status: int, category: str):
        super().__init__(f"HTTP {status} ({category})")
        self.status = status
        self.category = category


class RangeReader(io.RawIOBase):
    """Seekable file-like object over HTTP using *bounded* Range requests on a keep-alive
    connection, with an LRU block cache. Follows redirects once at open (signed-redirect hop).

    Accounting: .requests and .bytes_fetched (body bytes received).
    """

    def __init__(self, url: str, block: int = 256 * 1024, readahead_blocks: int = 8,
                 max_blocks: int = 256, timeout_s: float = 10.0, user_agent: str = UA,
                 max_redirects: int = 3):
        super().__init__()
        self.block = block
        self.readahead = readahead_blocks
        self.max_blocks = max_blocks
        self.timeout = timeout_s
        self.ua = user_agent
        self.cache: dict[int, bytes] = {}
        self.order: list[int] = []
        self.pos = 0
        self.requests = 0
        self.bytes_fetched = 0
        self.retries = 0
        self.redirects = 0
        self.conn = None
        self._last_block = -10
        self._run = 0
        self.url = url
        self.size = self._open(url, max_redirects)

    # -- http
    def _connect(self):
        p = urlsplit(self.url)
        cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        self.conn = cls(p.hostname, p.port, timeout=self.timeout)
        self.target = p.path + (f"?{p.query}" if p.query else "")

    def _request(self, start: int, end: int):
        for attempt in range(2):
            if self.conn is None:
                self._connect()
            try:
                self.conn.request("GET", self.target, headers={
                    "Range": f"bytes={start}-{end}", "User-Agent": self.ua})
                r = self.conn.getresponse()
                self.requests += 1
                return r
            except (http.client.HTTPException, OSError):
                self.conn.close()
                self.conn = None
                if attempt:
                    raise
        raise RuntimeError("unreachable")

    def _open(self, url: str, max_redirects: int) -> int:
        for _ in range(max_redirects + 1):
            self.url = url
            self.conn = None
            # first request doubles as the size probe and fetches block 0 (saves one round trip)
            r = self._request(0, self.block - 1)
            if r.status in (301, 302, 303, 307, 308):
                r.read()
                self.redirects += 1
                url = urljoin(url, r.getheader("Location"))
                continue
            if r.status == 206:
                cr = r.getheader("Content-Range") or ""
                try:
                    body = r.read()
                    self.bytes_fetched += len(body)
                    self._put(0, body)
                except (http.client.IncompleteRead, OSError) as e:
                    # dropped mid-body: the size is known from the headers; block 0 is refetched later
                    self.bytes_fetched += len(getattr(e, "partial", b"") or b"")
                    self.retries += 1
                    self.conn.close()
                    self.conn = None
                return int(cr.rsplit("/", 1)[1])
            # never drain a 200 (it would be the whole file) or an error body: drop the connection
            self.conn.close()
            self.conn = None
            if r.status == 200:
                raise HttpError(200, "no_range_support")
            raise HttpError(r.status, {403: "http_403_expired", 404: "http_404"}.get(r.status, "http_error"))
        raise HttpError(310, "too_many_redirects")

    def _fetch(self, b0: int, nblocks: int) -> None:
        start = b0 * self.block
        end = min(self.size, (b0 + nblocks) * self.block) - 1
        if start > end:
            return
        for attempt in range(3):
            r = self._request(start, end)
            if r.status != 206:
                r.read()
                raise HttpError(r.status, {403: "http_403_expired", 404: "http_404", 200: "no_range_support"}
                                .get(r.status, "http_error"))
            try:
                data = r.read()
                self.bytes_fetched += len(data)
                break
            except (http.client.IncompleteRead, OSError) as e:
                # connection dropped mid-body: count what arrived, reconnect, retry the range
                part = getattr(e, "partial", b"") or b""
                self.bytes_fetched += len(part)
                self.retries += 1
                self.conn.close()
                self.conn = None
                if attempt == 2:
                    raise
        for i in range(nblocks):
            chunk = data[i * self.block:(i + 1) * self.block]
            if not chunk:
                break
            self._put(b0 + i, chunk)

    def _put(self, b, data):
        self.cache[b] = data
        self.order.append(b)
        while len(self.order) > self.max_blocks:
            old = self.order.pop(0)
            if old not in self.order:
                self.cache.pop(old, None)

    def _get_block(self, b: int) -> bytes:
        if b not in self.cache:
            # random access (seek) -> fetch a single block; sustained sequential access ->
            # double the request size up to readahead_blocks (1, 2, 4, ...)
            self._run = self._run + 1 if b == self._last_block + 1 else 0
            n = min(self.readahead, 1 << self._run)
            n_missing = 0
            while n_missing < n and (b + n_missing) not in self.cache:
                n_missing += 1
            self._fetch(b, max(1, n_missing))
        self._last_block = b
        return self.cache[b]

    # -- io
    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, off, whence=0):
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        else:
            self.pos = self.size + off
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, buf):
        if self.pos >= self.size:
            return 0
        b = self.pos // self.block
        data = self._get_block(b)
        o = self.pos - b * self.block
        n = min(len(buf), len(data) - o)
        buf[:n] = data[o:o + n]
        self.pos += n
        return n

    def close(self):
        if self.conn:
            self.conn.close()
        super().close()


class AvExtractor:
    """Persistent in-process demuxer/decoder (S5). Not thread-safe; one per source."""

    def __init__(self, url: str, io_mode: str = "ranged", timeout_s: float = 10.0,
                 block: int = 256 * 1024, http: HttpOpts | None = None, thread_type: str = "SLICE",
                 thread_count: int = 0):
        import av  # optional dependency

        self.av = av
        self.reader = None
        self.t_open0 = time.perf_counter()
        http = http or HttpOpts(probesize=256 * 1024, analyzeduration_us=500_000)
        if io_mode == "ranged":
            self.reader = RangeReader(url, block=block, timeout_s=timeout_s)
            self.c = av.open(self.reader, mode="r", options={
                "probesize": str(http.probesize), "analyzeduration": str(http.analyzeduration_us)})
        else:
            self.c = av.open(url, mode="r", timeout=timeout_s, options={
                "probesize": str(http.probesize), "analyzeduration": str(http.analyzeduration_us),
                "user_agent": http.user_agent, "rw_timeout": str(int(timeout_s * 1e6))})
        self.vs = self.c.streams.video[0]
        # Frame threading (the libav default for "AUTO") delays the first decoded frame by
        # ~thread_count packets, which after every seek means reading that many extra packets
        # from the network. Slice threading has no such delay (measured: REPORT.md).
        self.vs.thread_type = thread_type
        if thread_count:
            self.vs.codec_context.thread_count = thread_count
        self.open_s = time.perf_counter() - self.t_open0
        trc = self.vs.codec_context.color_trc  # int (AVColorTransferCharacteristic)
        self.hdr = trc in (16, 18)

    def grab(self, t: float, out: str | Path, accurate: bool = True, keyframes_only: bool = False):
        """Seek to t and save a 320x180 letterboxed JPEG. Returns actual pts seconds."""
        from PIL import Image

        vs = self.vs
        tb = vs.time_base
        target = int(t / tb) + (vs.start_time or 0)
        self.c.seek(target, stream=vs, backward=True, any_frame=False)
        vs.codec_context.skip_frame = "NONKEY" if keyframes_only else "DEFAULT"
        frame = None
        for pkt in self.c.demux(vs):
            for f in pkt.decode():
                frame = f
                if not accurate or f.pts is None or f.pts >= target - 1:
                    break
            else:
                continue
            break
        if frame is None:
            raise RuntimeError("no frame")
        w, h = frame.width, frame.height
        s = min(THUMB_W / w, THUMB_H / h)
        sw, sh = max(2, int(round(w * s / 2) * 2)), max(2, int(round(h * s / 2) * 2))
        if self.hdr:
            img = self._tonemap(frame, sw, sh)
        else:
            img = frame.to_image(width=sw, height=sh, interpolation="BILINEAR")
        canvas = Image.new("RGB", (THUMB_W, THUMB_H))
        canvas.paste(img, ((THUMB_W - sw) // 2, (THUMB_H - sh) // 2))
        canvas.save(out, "JPEG", quality=80)
        return float((frame.pts - (vs.start_time or 0)) * tb)

    def _tonemap(self, frame, sw, sh):
        """HDR->SDR at thumbnail size in numpy (PyAV wheels ship without zscale).
        PQ (or HLG) -> linear (100 nit = 1.0) -> Hable (peak 1000 nit) -> BT.2020->BT.709 -> gamma 2.4."""
        import numpy as np
        from PIL import Image

        f = frame.reformat(width=sw, height=sh, format="rgb48le", src_colorspace="BT2020",
                           interpolation="BILINEAR")
        e = f.to_ndarray().astype(np.float32) / 65535.0
        if self.vs.codec_context.color_trc == 16:  # PQ
            m1, m2 = 2610 / 16384, 2523 / 4096 * 128
            c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
            p = np.power(np.clip(e, 0, 1), 1 / m2)
            lin = np.power(np.maximum(p - c1, 0) / (c2 - c3 * p), 1 / m1) * 100.0  # 1.0 = 100 nit
        else:  # HLG inverse OETF, scaled to a 1000 nit display
            a, b, c = 0.17883277, 0.28466892, 0.55991073
            lin = np.where(e <= 0.5, e * e / 3, (np.exp((e - c) / a) + b) / 12) * 10.0

        def hable(x):
            A, B, C, D, E, F = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30
            return (x * (A * x + C * B) + D * E) / (x * (A * x + B) + D * F) - E / F

        tm = hable(lin) / hable(np.float32(10.0))
        m = np.array([[1.6605, -0.5876, -0.0728], [-0.1246, 1.1329, -0.0083], [-0.0182, -0.1006, 1.1187]],
                     dtype=np.float32)
        rgb = np.clip(tm @ m.T, 0, 1)
        out = (np.power(rgb, 1 / 2.4) * 255 + 0.5).astype(np.uint8)
        return Image.fromarray(out, "RGB")

    def close(self):
        self.c.close()
        if self.reader:
            self.reader.close()


# ---------------------------------------------------------------- CLI
def _s5_worker(url: str, ts: list[float], out: Path, accurate: bool, block: int, io_mode: str) -> int:
    """JSON-lines worker used by run_bench.py (so CPU/RSS can be measured per process)."""
    t0 = time.perf_counter()
    try:
        ex = AvExtractor(url, io_mode=io_mode, block=block)
    except HttpError as e:
        print(json.dumps({"event": "error", "error": e.category, "status": e.status}), flush=True)
        return 3
    except Exception as e:  # noqa: BLE001 - report category, never the URL
        print(json.dumps({"event": "error", "error": "open_failed:" + type(e).__name__}), flush=True)
        return 3
    rd = ex.reader
    print(json.dumps({"event": "open", "s": round(time.perf_counter() - t0, 4), "hdr": ex.hdr,
                      "requests": rd.requests if rd else None, "bytes": rd.bytes_fetched if rd else None}),
          flush=True)
    rc = 0
    for i, t in enumerate(ts):
        t1 = time.perf_counter()
        q0, b0 = (rd.requests, rd.bytes_fetched) if rd else (0, 0)
        o = out / f"f{i:04d}.jpg"
        try:
            pts = ex.grab(t, o, accurate=accurate)
            ev = {"event": "frame", "i": i, "t": t, "pts": round(pts, 4), "out": str(o)}
        except HttpError as e:
            ev = {"event": "frame", "i": i, "t": t, "error": e.category}
            rc = 4
        except Exception as e:  # noqa: BLE001
            ev = {"event": "frame", "i": i, "t": t, "error": "grab_failed:" + type(e).__name__}
            rc = 4
        ev["s"] = round(time.perf_counter() - t1, 4)
        ev["since_start"] = round(time.perf_counter() - t0, 4)
        if rd:
            ev["requests"] = rd.requests - q0
            ev["bytes"] = rd.bytes_fetched - b0
            ev["retries"] = rd.retries
        print(json.dumps(ev), flush=True)
    ex.close()
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract seek-preview frames (never prints the URL)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--url-env", help="name of env var holding the URL (preferred: keeps it out of argv)")
    ap.add_argument("--t", required=True, help="comma-separated seconds")
    ap.add_argument("--out", default="out")
    ap.add_argument("--strategy", default="s2", choices=["s1", "s2", "s5", "s5key"])
    ap.add_argument("--io", default="ranged", choices=["ranged", "libav"])
    ap.add_argument("--block", type=int, default=256 * 1024)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--no-probe", action="store_true")
    a = ap.parse_args()
    url = a.url or os.environ.get(a.url_env, "")
    if not url:
        print("BLOCKED: no URL supplied", file=sys.stderr)
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = [float(x) for x in a.t.split(",")]
    if a.strategy.startswith("s5"):
        return _s5_worker(url, ts, out, a.strategy == "s5", a.block, a.io)
    pf = preflight(url)
    pr = probe(url)
    print(json.dumps({"source": redact_url(url, host_only=True), "preflight": pf, "probe": {
        k: v for k, v in asdict(pr).items() if k != "proc"}, "probe_s": round(pr.proc.wall_s, 3)}))
    if not pr.ok:
        return 1
    for i, t in enumerate(ts):
        r = ffmpeg_frame(url, t, out / f"f{i:04d}.jpg", hdr=pr.hdr,
                         accurate=a.strategy == "s1", timeout_s=a.timeout)
        print(json.dumps({"t": t, "ok": r.ok, "error": r.error, "wall_s": round(r.proc.wall_s, 3),
                          "cpu_s": r.proc.cpu_s, "peak_rss": r.proc.peak_rss}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
