"""OpenSeek Gate 1 benchmark runner.

Runs the experiment matrix against the in-process test server (server.py) and fixtures
(fixtures.py), writes results/results.json and results/REPORT.md.

  python run_bench.py                       # everything except the internet + TorBox parts
  python run_bench.py --exp internet        # real public sample (Blender Sintel MKV, Jellyfin 4K HDR10)
  python run_bench.py --exp torbox --url-env OPENSEEK_BENCH_URL   # your real REMUX link (never printed)
  python run_bench.py --report-only         # regenerate REPORT.md from results.json

Experiments: env, containers, strategies, gop10, hdr, sequential, failures, concurrency,
playback, scrub, scrub_seq (long: ~25 min), internet, torbox
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import http.client
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import barcode  # noqa: E402
import extract  # noqa: E402
from extract import HttpOpts  # noqa: E402
from redact import redact_url  # noqa: E402
from server import BenchServer  # noqa: E402

FIX = HERE / "fixtures"
RES = HERE / "results"
FRAMES = RES / "frames"
MANIFEST = json.loads((FIX / "manifest.json").read_text()) if (FIX / "manifest.json").exists() else {}

PROFILES = {
    "lan": {},  # localhost, unthrottled: CPU / protocol floor
    "cdn": {"rate_mbps": 100, "latency_ms": 40, "new_conn_rtts": 2},  # remote CDN over a 100 Mbit/s link
}

# Per-process ffmpeg strategy variants (S1/S2)
MB = 1 << 20
FF_STRATEGIES = {
    "S1-accurate": dict(accurate=True),
    "S2-noaccurate": dict(accurate=False),
    "S2-nokey": dict(accurate=True, skip_nokey=True),
    "S2-noacc-nokey": dict(accurate=False, skip_nokey=True),
    "S2-noacc-rs1M": dict(accurate=False, http=HttpOpts(request_size=MB, multiple_requests=True)),
    "S2-noacc-probe256k": dict(accurate=False, http=HttpOpts(probesize=256 * 1024, analyzeduration_us=500_000)),
    "S2-tuned": dict(accurate=False, http=HttpOpts(request_size=MB, multiple_requests=True,
                                                    probesize=256 * 1024, analyzeduration_us=500_000)),
    "S1-tuned": dict(accurate=True, http=HttpOpts(request_size=MB, multiple_requests=True,
                                                   probesize=256 * 1024, analyzeduration_us=500_000)),
    "S2-tuned-slice": dict(accurate=False, thread_type="slice",
                           http=HttpOpts(request_size=MB, multiple_requests=True,
                                         probesize=256 * 1024, analyzeduration_us=500_000)),
    "S2-tuned-slice-rs512k": dict(accurate=False, thread_type="slice",
                                  http=HttpOpts(request_size=MB // 2, multiple_requests=True,
                                                probesize=256 * 1024, analyzeduration_us=500_000)),
    "S1-tuned-slice": dict(accurate=True, thread_type="slice",
                           http=HttpOpts(request_size=MB, multiple_requests=True,
                                         probesize=256 * 1024, analyzeduration_us=500_000)),
}

RESULTS: dict = {}


def log(*a):
    print(*a, flush=True)


def save():
    RES.mkdir(parents=True, exist_ok=True)
    (RES / "results.json").write_text(json.dumps(RESULTS, indent=1, default=str))


def spread(duration: float, n: int, jitter: float = 0.37) -> list[float]:
    """n timestamps spread over the file; the per-index offset varies the position inside the
    GOP (0.37 .. 1.9 s after a 2 s-GOP keyframe) so keyframe-snapping error is exercised."""
    return [round(duration * (i + 0.5) / n + jitter + (i * 0.731) % 1.6, 3) for i in range(n)]


def check_frame(path: str | None, t: float) -> dict:
    if not path or not os.path.exists(path):
        return {"t": t, "got": None, "err_s": None}
    n, _ = barcode.decode_file(path)
    if n is None:
        return {"t": t, "got": None, "err_s": None, "undecodable": True}
    got = n / barcode.FPS
    return {"t": t, "got": round(got, 4), "err_s": round(got - t, 4)}


def acc_summary(checks: list[dict]) -> dict:
    errs = [c["err_s"] for c in checks if c.get("err_s") is not None]
    if not errs:
        return {"n": 0}
    return {"n": len(errs), "undecoded": len(checks) - len(errs), "min_err_s": min(errs),
            "max_err_s": max(errs), "max_abs_err_s": max(abs(e) for e in errs),
            "mean_abs_err_s": round(statistics.mean(abs(e) for e in errs), 4)}


class Bench:
    def __init__(self):
        self.srv = BenchServer(FIX).start()
        FRAMES.mkdir(parents=True, exist_ok=True)

    def url(self, fixture: str, prefix: str = "/f/") -> str:
        return f"{self.srv.base}{prefix}{fixture}"

    def totals(self, ua: str | None = None) -> tuple[int, int, int]:
        st = self.srv.stats()
        if ua is None:
            return st["bytes"], st["requests"], st["connections"]
        u = st["by_ua"].get(ua, {"bytes": 0, "requests": 0})
        return u["bytes"], u["requests"], 0

    # ---------------------------------------------------------------- per-process ffmpeg (S1/S2)
    def ff_frames(self, fixture: str, ts: list[float], strategy: str, profile: str, *,
                  prefix: str = "/f/", hdr: bool = False, hdr_mode: str = "small", timeout_s: float = 120,
                  server_cfg: dict | None = None, keep_log: bool = True, url: str | None = None,
                  verify: bool = True) -> dict:
        kw = dict(FF_STRATEGIES[strategy])
        cfg = dict(PROFILES[profile])
        cfg.update(server_cfg or {})
        self.srv.reset(**cfg)
        url = url or self.url(fixture, prefix)
        outdir = FRAMES / f"{fixture}_{strategy}_{profile}"
        outdir.mkdir(parents=True, exist_ok=True)
        frames = []
        for i, t in enumerate(ts):
            b0, r0, c0 = self.totals()
            r = extract.ffmpeg_frame(url, t, outdir / f"f{i:04d}.jpg", hdr=hdr, hdr_mode=hdr_mode,
                                     timeout_s=timeout_s, **kw)
            b1, r1, c1 = self.totals()
            fr = {"t": t, "ok": r.ok, "error": r.error, "wall_s": round(r.proc.wall_s, 4),
                  "cpu_s": r.proc.cpu_s, "peak_rss": r.proc.peak_rss, "bytes": b1 - b0,
                  "requests": r1 - r0, "connections": c1 - c0, "orphan": r.proc.orphan}
            if verify:
                fr["check"] = check_frame(r.out, t)
            if not r.ok:
                fr["stderr"] = r.proc.stderr[-400:]
            frames.append(fr)
        st = self.srv.stats()
        rec = summarize_frames(frames)
        rec.update(fixture=fixture, strategy=strategy, profile=profile, server_cfg=cfg,
                   file_bytes=MANIFEST.get(fixture, {}).get("bytes"),
                   server_bytes=st["bytes"], server_requests=st["requests"],
                   server_connections=st["connections"], frames=frames)
        if keep_log:
            rec["request_log"] = [{k: l[k] for k in ("t", "range", "status", "bytes", "aborted", "conn")}
                                  for l in st["log"][:40]]
        return rec

    # ---------------------------------------------------------------- S5 (PyAV) worker process
    def s5(self, fixture: str, ts: list[float], profile: str, *, accurate: bool = False,
           prefix: str = "/f/", server_cfg: dict | None = None, timeout_s: float = 300,
           url: str | None = None, block: int = 256 * 1024, io_mode: str = "ranged",
           verify: bool = True, reset: bool = True) -> dict:
        cfg = dict(PROFILES[profile])
        cfg.update(server_cfg or {})
        if reset:
            self.srv.reset(**cfg)
        url = url or self.url(fixture, prefix)
        tag = "S5-accurate" if accurate else "S5-keyframe"
        if io_mode != "ranged":
            tag += "-libavio"
        outdir = FRAMES / f"{fixture}_{tag}_{profile}_{threading.get_ident()}"
        outdir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["OPENSEEK_WORKER_URL"] = url
        sink: list[str] = []
        args = [sys.executable, str(HERE / "extract.py"), "--url-env", "OPENSEEK_WORKER_URL",
                "--strategy", "s5" if accurate else "s5key", "--io", io_mode, "--block", str(block),
                "--t", ",".join(str(t) for t in ts), "--out", str(outdir)]
        p = extract.run_proc(args, timeout_s, env=env, stdout_sink=sink)
        evs = [json.loads(l) for l in sink[0].splitlines() if l.startswith("{")] if sink else []
        st = self.srv.stats() if reset else None
        opened = next((e for e in evs if e["event"] == "open"), None)
        frames = []
        for e in evs:
            if e["event"] != "frame":
                continue
            fr = {"t": e["t"], "ok": "error" not in e, "error": e.get("error"), "wall_s": e["s"], "out": e.get("out"),
                  "since_start": e["since_start"], "bytes": e.get("bytes"), "requests": e.get("requests"),
                  "pts": e.get("pts")}
            if verify:
                fr["check"] = check_frame(e.get("out"), e["t"])
            frames.append(fr)
        err = next((e["error"] for e in evs if e["event"] == "error"), None)
        if p.killed:
            err = p.killed
        oks = [f for f in frames if f["ok"]]
        rec = {
            "fixture": fixture, "strategy": tag, "profile": profile, "server_cfg": cfg,
            "file_bytes": MANIFEST.get(fixture, {}).get("bytes"),
            "n": len(ts), "ok": len(oks), "error": err,
            "process_wall_s": round(p.wall_s, 4), "cpu_s": p.cpu_s, "peak_rss": p.peak_rss,
            "open_s": opened["s"] if opened else None,
            "open_bytes": opened["bytes"] if opened else None,
            "open_requests": opened["requests"] if opened else None,
            "cold_first_s": frames[0]["since_start"] if frames else None,
            "warm_frame_s_mean": round(statistics.mean(f["wall_s"] for f in oks[1:]), 4) if len(oks) > 1 else None,
            "warm_frame_s_p95": pct([f["wall_s"] for f in oks[1:]], 95) if len(oks) > 2 else None,
            "frames": frames,
            "accuracy": acc_summary([f["check"] for f in frames if "check" in f]) if verify else None,
            "stderr": p.stderr[-400:] if p.returncode not in (0, None) else "",
        }
        if st:
            rec.update(server_bytes=st["bytes"], server_requests=st["requests"],
                       server_connections=st["connections"],
                       bytes_per_frame=round(st["bytes"] / max(1, len(oks))),
                       requests_per_frame=round(st["requests"] / max(1, len(oks)), 2),
                       cpu_s_per_frame=round((p.cpu_s or 0) / max(1, len(oks)), 4))
            rec["request_log"] = [{k: l[k] for k in ("t", "range", "status", "bytes", "aborted", "conn")}
                                  for l in st["log"][:40]]
        return rec


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = min(len(xs) - 1, max(0, int(round((p / 100) * (len(xs) - 1)))))
    return round(xs[k], 4)


def summarize_frames(frames: list[dict]) -> dict:
    oks = [f for f in frames if f["ok"]]
    n_ok = len(oks)
    walls = [f["wall_s"] for f in frames]
    out = {
        "n": len(frames), "ok": n_ok,
        "errors": sorted({f["error"] for f in frames if f["error"]}),
        "total_wall_s": round(sum(walls), 4),
        "cold_first_s": walls[0] if walls else None,
        "frame_s_mean": round(statistics.mean(walls), 4) if walls else None,
        "frame_s_p95": pct(walls, 95),
        "bytes_total": sum(f["bytes"] for f in frames),
        "bytes_per_frame": round(sum(f["bytes"] for f in frames) / max(1, n_ok)),
        "requests_per_frame": round(sum(f["requests"] for f in frames) / max(1, n_ok), 2),
        "connections_per_frame": round(sum(f.get("connections", 0) for f in frames) / max(1, n_ok), 2),
        "cpu_s_per_frame": round(sum((f["cpu_s"] or 0) for f in frames) / max(1, n_ok), 4),
        "peak_rss_max": max((f["peak_rss"] or 0) for f in frames) if frames else None,
        "orphans": sum(1 for f in frames if f.get("orphan")),
    }
    checks = [f["check"] for f in frames if "check" in f]
    if checks:
        out["accuracy"] = acc_summary(checks)
    return out


# ==================================================================== experiments
def exp_env(b: Bench):
    v = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    filt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    import av
    import PIL

    RESULTS["env"] = {
        "platform": platform.platform(), "cpu_count": os.cpu_count(), "python": sys.version.split()[0],
        "ffmpeg": v, "zscale": " zscale " in filt, "libplacebo": " libplacebo " in filt,
        "tonemap": " tonemap " in filt, "pyav": av.__version__, "pyav_libavformat": av.library_versions["libavformat"],
        "pyav_has_zscale": "zscale" in av.filter.filters_available, "pillow": PIL.__version__,
        "psutil": psutil.__version__, "profiles": PROFILES, "fixtures": MANIFEST,
        "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
    }


def exp_containers(b: Bench):
    """Index location matters: Cues end/front/none, MP4 moov end/front, the big REMUX-like file."""
    out = []
    cases = [("med_cues_end.mkv", 200.37), ("med_cues_front.mkv", 200.37), ("med_no_cues.mkv", 200.37),
             ("med_moov_end.mp4", 200.37), ("med_faststart.mp4", 200.37), ("remux_like.mkv", 4000.37)]
    if QUICK:
        cases = [c for c in cases if c[0] in ("med_cues_end.mkv", "med_cues_front.mkv", "med_moov_end.mp4",
                                              "remux_like.mkv")]
    for fx, t in cases:
        for strat in ("S1-accurate", "S2-noaccurate", "S2-tuned-slice"):
            r = b.ff_frames(fx, [t], strat, "cdn", timeout_s=180)
            log(f"[containers] {fx:20s} {strat:16s} ok={r['ok']} {r['cold_first_s']:.2f}s "
                f"{r['bytes_total'] / 1e6:.2f}MB req={r['server_requests']} err={r['errors']}")
            out.append(r)
        r = b.s5(fx, [t], "cdn", timeout_s=180)
        log(f"[containers] {fx:20s} S5-keyframe      ok={r['ok']} {r['cold_first_s']}s "
            f"{r.get('server_bytes', 0) / 1e6:.2f}MB req={r.get('server_requests')} err={r['error']}")
        out.append(r)
        RESULTS["containers"] = out
        save()


def exp_strategies(b: Bench):
    """Strategy comparison on the REMUX-like file, 1 -> 5 -> 20 frames (scaled only if affordable)."""
    fx = "remux_like.mkv"
    dur = MANIFEST[fx]["duration"]
    out = []
    for profile in ("cdn", "lan"):
        for n in (1, 5, 20):
            ts = spread(dur, n)
            for strat in ("S1-accurate", "S2-noaccurate", "S2-nokey", "S2-noacc-nokey", "S2-noacc-rs1M",
                          "S2-noacc-probe256k", "S2-tuned", "S2-tuned-slice", "S2-tuned-slice-rs512k",
                          "S1-tuned", "S1-tuned-slice"):
                if profile == "lan" and strat not in ("S1-accurate", "S2-noaccurate", "S2-tuned-slice"):
                    continue
                if n == 20 and strat not in ("S1-accurate", "S2-tuned-slice", "S1-tuned-slice"):
                    continue
                r = b.ff_frames(fx, ts, strat, profile, keep_log=(n == 1))
                r["n_frames"] = n
                log(f"[strategies] {profile} n={n:2d} {strat:18s} first={r['cold_first_s']:.2f}s "
                    f"mean={r['frame_s_mean']:.2f}s {r['bytes_per_frame'] / 1e6:.2f}MB/f "
                    f"{r['requests_per_frame']}req/f cpu={r['cpu_s_per_frame']}s acc={r.get('accuracy')}")
                out.append(r)
            # S3: one ffmpeg, one input per timestamp
            if n > 1:
                for acc in (False, True):
                    b.srv.reset(**PROFILES[profile])
                    od = FRAMES / f"s3_{profile}_{n}_{acc}"
                    got, st, err = extract.ffmpeg_multi(b.url(fx), ts, od, accurate=acc, timeout_s=300)
                    s = b.srv.stats()
                    checks = [check_frame(g, t) for g, t in zip(got, ts)]
                    r = {"fixture": fx, "strategy": "S3-multi-input" + ("-accurate" if acc else "-noacc"),
                         "profile": profile, "n_frames": n, "n": n, "ok": sum(1 for g in got if g),
                         "error": err, "total_wall_s": round(st.wall_s, 3), "cpu_s": st.cpu_s,
                         "cpu_s_per_frame": round((st.cpu_s or 0) / n, 4), "peak_rss_max": st.peak_rss,
                         "bytes_total": s["bytes"], "bytes_per_frame": round(s["bytes"] / n),
                         "requests_per_frame": round(s["requests"] / n, 2),
                         "connections_per_frame": round(s["connections"] / n, 2),
                         "frame_s_mean": round(st.wall_s / n, 4), "accuracy": acc_summary(checks)}
                    log(f"[strategies] {profile} n={n:2d} {r['strategy']:18s} total={st.wall_s:.2f}s "
                        f"{r['bytes_per_frame'] / 1e6:.2f}MB/f rss={(st.peak_rss or 0) / 1e6:.0f}MB acc={r['accuracy']}")
                    out.append(r)
            for acc in (False, True):
                r = b.s5(fx, ts, profile, accurate=acc)
                r["n_frames"] = n
                log(f"[strategies] {profile} n={n:2d} {r['strategy']:18s} first={r['cold_first_s']}s "
                    f"warm={r['warm_frame_s_mean']}s {r.get('bytes_per_frame', 0) / 1e6:.2f}MB/f "
                    f"{r.get('requests_per_frame')}req/f cpu/f={r.get('cpu_s_per_frame')} "
                    f"rss={(r['peak_rss'] or 0) / 1e6:.0f}MB acc={r['accuracy']} err={r['error']}")
                out.append(r)
            if n == 5 and profile == "cdn":
                for blk in (128 * 1024, 512 * 1024, 1024 * 1024):
                    r = b.s5(fx, ts, profile, accurate=False, block=blk)
                    r["strategy"] += f"-block{blk // 1024}k"
                    r["n_frames"] = n
                    log(f"[strategies] cdn n=5 {r['strategy']:24s} warm={r['warm_frame_s_mean']}s "
                        f"{r.get('bytes_per_frame', 0) / 1e6:.2f}MB/f {r.get('requests_per_frame')}req/f")
                    out.append(r)
            RESULTS["strategies"] = out
            save()
    # local-file baseline (no network at all): CPU floor of the same ffmpeg command
    loc = []
    ts = spread(dur, 5)
    for strat in ("S1-accurate", "S2-noaccurate", "S2-tuned-slice", "S1-tuned-slice"):
        r = b.ff_frames(fx, ts, strat, "lan", url=str(FIX / fx))
        r["strategy"] += "-localfile"
        loc.append(r)
        log(f"[strategies] localfile {strat} mean={r['frame_s_mean']}s cpu={r['cpu_s_per_frame']}")
    RESULTS["strategies_localfile"] = loc
    # PyAV with libav's own http IO (for comparison; small file only - it degrades badly)
    r = b.s5("small.mkv", spread(60, 3), "cdn", io_mode="libav", timeout_s=120)
    log(f"[strategies] S5 libav-http IO small.mkv bytes={r.get('server_bytes')} first={r['cold_first_s']}")
    RESULTS["s5_libavio"] = r
    save()


def exp_gop10(b: Bench):
    fx = "gop10.mkv"
    ts = spread(MANIFEST[fx]["duration"], 5)
    out = []
    for strat in ("S1-accurate", "S2-noaccurate", "S2-tuned-slice", "S2-nokey", "S1-tuned-slice"):
        r = b.ff_frames(fx, ts, strat, "cdn", keep_log=False)
        log(f"[gop10] {strat:16s} mean={r['frame_s_mean']}s {r['bytes_per_frame'] / 1e6:.2f}MB/f acc={r.get('accuracy')}")
        out.append(r)
    for acc in (False, True):
        r = b.s5(fx, ts, "cdn", accurate=acc)
        log(f"[gop10] {r['strategy']:16s} warm={r['warm_frame_s_mean']}s {r.get('bytes_per_frame', 0) / 1e6:.2f}MB/f acc={r['accuracy']}")
        out.append(r)
    RESULTS["gop10"] = out
    save()


def exp_hdr(b: Bench):
    fx = "hdr4k.mkv"
    ts = spread(MANIFEST[fx]["duration"], 5)
    out = []
    from PIL import Image, ImageStat

    for mode in ("small", "full", "placebo", "none"):
        for strat in ("S2-tuned-slice", "S1-tuned-slice"):
            r = b.ff_frames(fx, ts, strat, "cdn", hdr=mode != "none", hdr_mode=mode if mode != "none" else "small",
                            keep_log=False)
            r["hdr_mode"] = mode
            lum = []
            for i in range(len(ts)):
                p = FRAMES / f"{fx}_{strat}_cdn" / f"f{i:04d}.jpg"
                if p.exists():
                    lum.append(round(ImageStat.Stat(Image.open(p).convert("L")).mean[0], 1))
            r["mean_luma"] = lum
            log(f"[hdr] {mode:8s} {strat} mean={r['frame_s_mean']}s cpu/f={r['cpu_s_per_frame']} "
                f"rss={(r['peak_rss_max'] or 0) / 1e6:.0f}MB acc={r.get('accuracy')} err={r['errors']}")
            out.append(r)
    for acc in (False, True):
        r = b.s5(fx, ts, "cdn", accurate=acc)
        r["hdr_mode"] = "numpy-hable"
        log(f"[hdr] S5 {r['strategy']} warm={r['warm_frame_s_mean']} cpu/f={r.get('cpu_s_per_frame')} acc={r['accuracy']}")
        out.append(r)
    RESULTS["hdr"] = out
    save()


def exp_sequential(b: Bench):
    """S4 naive full decode with fps filter (affordable fixture only)."""
    fx = "med_cues_end.mkv"
    out = []
    for profile in ("lan", "cdn"):
        b.srv.reset(**PROFILES[profile])
        od = FRAMES / f"s4_{profile}"
        got, st, err = extract.ffmpeg_sequential(b.url(fx), 10, od, timeout_s=900)
        s = b.srv.stats()
        checks = [check_frame(g, i * 10.0) for i, g in enumerate(got)]
        r = {"fixture": fx, "strategy": "S4-sequential-fps", "profile": profile, "interval_s": 10,
             "frames": len(got), "error": err, "wall_s": round(st.wall_s, 2), "cpu_s": st.cpu_s,
             "peak_rss": st.peak_rss, "bytes": s["bytes"], "file_bytes": MANIFEST[fx]["bytes"],
             "bytes_fraction_of_file": round(s["bytes"] / MANIFEST[fx]["bytes"], 4),
             "requests": s["requests"], "accuracy": acc_summary(checks),
             "media_s_per_wall_s": round(MANIFEST[fx]["duration"] / st.wall_s, 2)}
        log(f"[sequential] {profile} {r}")
        out.append(r)
    RESULTS["sequential"] = out
    save()


def exp_failures(b: Bench):
    out = {}
    fx = "med_cues_end.mkv"
    T = 200.37
    # 1. server without Range support
    rows = []
    pf = extract.preflight(b.url(fx))
    b.srv.reset(range=False, **PROFILES["cdn"])
    pf_nr = extract.preflight(b.url(fx))
    pf_nr_stats = b.srv.stats()
    rows.append({"case": "preflight", "range_server": pf, "norange_server": pf_nr,
                 "norange_preflight_bytes": pf_nr_stats["bytes"]})
    for strat in (() if QUICK else ("S1-accurate", "S2-noaccurate")):
        r = b.ff_frames(fx, [T], strat, "cdn", server_cfg={"range": False}, timeout_s=180)
        rows.append(r)
        log(f"[norange] {strat} ok={r['ok']} {r['cold_first_s']}s {r['bytes_total'] / 1e6:.1f}MB "
            f"({r['bytes_total'] / MANIFEST[fx]['bytes']:.0%} of file) acc={r.get('accuracy')} err={r['errors']}")
    if not QUICK:
        r = b.ff_frames("med_faststart.mp4", [T], "S2-noaccurate", "cdn", server_cfg={"range": False},
                        timeout_s=180)
        rows.append(r)
        log(f"[norange] mp4-faststart S2 ok={r['ok']} {r['cold_first_s']}s {r['bytes_total'] / 1e6:.1f}MB")
    r = b.s5(fx, [T], "cdn", server_cfg={"range": False})
    rows.append(r)
    log(f"[norange] S5 err={r['error']} bytes={r.get('server_bytes')}")
    out["no_range"] = rows
    # 2. signed redirect hop
    rows = []
    ts = spread(MANIFEST[fx]["duration"], 5)
    for strat in ("S2-noaccurate", "S2-tuned-slice"):
        r = b.ff_frames(fx, ts, strat, "cdn", prefix="/r/")
        st = b.srv.stats()
        r["redirects_302"] = sum(1 for l in st["log"] if l["status"] == 302)
        r["logged_paths_sample"] = sorted({l["path"] for l in st["log"]})[:3]
        rows.append(r)
        log(f"[redirect] {strat} ok={r['ok']}/{r['n']} 302s={r['redirects_302']} mean={r['frame_s_mean']}s "
            f"paths={r['logged_paths_sample']}")
    r = b.s5(fx, ts, "cdn", prefix="/r/")
    st = b.srv.stats()
    r["redirects_302"] = sum(1 for l in st["log"] if l["status"] == 302)
    rows.append(r)
    log(f"[redirect] S5 ok={r['ok']}/{r['n']} 302s={r['redirects_302']} warm={r['warm_frame_s_mean']}")
    out["redirect"] = rows
    # 3. expired link (403 after N requests / after T seconds)
    rows = []
    r = b.ff_frames(fx, ts, "S2-tuned-slice", "cdn", server_cfg={"expire_after_requests": 8})
    rows.append(r)
    log(f"[expired] S2 after 8 req: ok={r['ok']}/{r['n']} errors={r['errors']} "
        f"walls={[f['wall_s'] for f in r['frames']]}")
    r = b.s5(fx, ts, "cdn", server_cfg={"expire_after_requests": 4})
    rows.append(r)
    log(f"[expired] S5 after 4 req: ok={r['ok']}/{r['n']} errors={[f['error'] for f in r['frames']]}")
    r = b.ff_frames(fx, ts[:2], "S2-tuned-slice", "cdn", server_cfg={"expire_after_s": 0.001})
    rows.append(r)
    log(f"[expired] S2 time-expired: errors={r['errors']} walls={[f['wall_s'] for f in r['frames']]}")
    out["expired"] = rows
    # 4. dropped connection mid-body
    rows = []
    for strat, http in (("S2-noaccurate", None), ("S2-tuned-slice", None)):
        r = b.ff_frames(fx, [T], strat, "cdn", server_cfg={"drop_after_bytes": 200_000, "drops": 1})
        rows.append(r)
        log(f"[drop] {strat} (reconnect on) ok={r['ok']} err={r['errors']} {r['cold_first_s']}s req={r['server_requests']}")
    FF_STRATEGIES["S2-noreconnect"] = dict(accurate=False, http=HttpOpts(reconnect=False))
    r = b.ff_frames(fx, [T], "S2-noreconnect", "cdn", server_cfg={"drop_after_bytes": 200_000, "drops": -1})
    rows.append(r)
    log(f"[drop] S2 no-reconnect, every response dropped: ok={r['ok']} err={r['errors']} acc={r.get('accuracy')}")
    r = b.s5(fx, [T, T + 20], "cdn", server_cfg={"drop_after_bytes": 200_000, "drops": 2})
    rows.append(r)
    log(f"[drop] S5 ok={r['ok']} err={r['error']} frames={[(f['error'], f.get('check')) for f in r['frames']]}")
    out["dropped"] = rows
    # 5. bad inputs, timeouts, cancellation
    rows = []
    b.srv.reset()
    r = extract.ffmpeg_frame(b.url("does_not_exist.mkv"), 1, FRAMES / "x404.jpg")
    rows.append({"case": "404", "ok": r.ok, "error": r.error, "wall_s": round(r.proc.wall_s, 3)})
    r = extract.ffmpeg_frame(b.url("manifest.json"), 1, FRAMES / "xjson.jpg")
    rows.append({"case": "not_a_video", "ok": r.ok, "error": r.error, "wall_s": round(r.proc.wall_s, 3)})
    r = extract.ffmpeg_frame(b.url(fx), 99999, FRAMES / "xpast.jpg", accurate=False)
    rows.append({"case": "t_beyond_duration", "ok": r.ok, "error": r.error, "wall_s": round(r.proc.wall_s, 3)})
    b.srv.reset(latency_ms=6000)
    r = extract.ffmpeg_frame(b.url(fx), 10, FRAMES / "xslow.jpg", http=HttpOpts(rw_timeout_s=1.5), timeout_s=30)
    rows.append({"case": "server_stall_rw_timeout_1.5s", "ok": r.ok, "error": r.error,
                 "wall_s": round(r.proc.wall_s, 3), "orphan": r.proc.orphan})
    r = extract.ffmpeg_frame(b.url(fx), 10, FRAMES / "xslow.jpg", http=HttpOpts(rw_timeout_s=60), timeout_s=2.0)
    rows.append({"case": "hard_wallclock_kill_2s", "ok": r.ok, "error": r.error, "killed": r.proc.killed,
                 "wall_s": round(r.proc.wall_s, 3), "orphan": r.proc.orphan})
    b.srv.reset(**PROFILES["cdn"], range=False)
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    r = extract.ffmpeg_frame(b.url(fx), 200, FRAMES / "xcancel.jpg", cancel=ev, timeout_s=60)
    rows.append({"case": "cancel_after_0.5s", "ok": r.ok, "error": r.error, "killed": r.proc.killed,
                 "wall_s": round(r.proc.wall_s, 3), "orphan": r.proc.orphan})
    for row in rows:
        log(f"[bad-input] {row}")
    out["bad_input"] = rows
    RESULTS["failures"] = out
    save()


# ---------------------------------------------------------------- playback proxy client
class Playback(threading.Thread):
    """Simulated player streaming the same file through the same throttled link.

    Downloads as fast as allowed while its buffer is below max_buffer_s, plays out at the
    media bitrate once start_buffer_s is buffered, records stalls (rebuffering) and startup time.
    """

    def __init__(self, base: str, path: str, bitrate_bps: float, offset: int, duration_s: float,
                 start_buffer_s: float = 2.5, max_buffer_s: float = 30.0):
        super().__init__(daemon=True)
        self.base, self.path = base, path
        self.rate = bitrate_bps / 8
        self.offset = offset
        self.duration = duration_s
        self.start_buf, self.max_buf = start_buffer_s, max_buffer_s
        self.samples: list[tuple[float, int]] = []
        self.startup_s = None
        self.stalls = 0
        self.stall_s = 0.0
        self.bytes = 0

    def run(self):
        host, port = self.base.split("//")[1].split(":")
        c = http.client.HTTPConnection(host, int(port), timeout=10)
        c.request("GET", self.path, headers={"Range": f"bytes={self.offset}-", "User-Agent": "playback"})
        r = c.getresponse()
        t0 = time.perf_counter()
        played = 0.0
        playing = False
        last = t0
        stall_start = None
        while True:
            now = time.perf_counter()
            el = now - t0
            if el > self.duration:
                break
            dt = now - last
            last = now
            if playing:
                played += dt
            buffered = self.bytes / self.rate - played
            if playing and buffered <= 0:
                playing = False
                self.stalls += 1
                stall_start = now
                played = self.bytes / self.rate
                buffered = 0
            if not playing and buffered >= self.start_buf:
                playing = True
                if self.startup_s is None:
                    self.startup_s = round(el, 3)
                if stall_start is not None:
                    self.stall_s += now - stall_start
                    stall_start = None
            if buffered < self.max_buf:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                self.bytes += len(chunk)
                self.samples.append((el, self.bytes))
            else:
                time.sleep(0.01)
        if stall_start is not None:
            self.stall_s += time.perf_counter() - stall_start
        c.close()

    def throughput(self, a: float, z: float) -> float | None:
        pts = [(t, b) for t, b in self.samples if a <= t <= z]
        if len(pts) < 2:
            return None
        return (pts[-1][1] - pts[0][1]) * 8 / max(1e-6, pts[-1][0] - pts[0][0])


def exp_concurrency(b: Bench):
    fx = "remux_like.mkv"
    dur = MANIFEST[fx]["duration"]
    ts_all = spread(dur, 16, jitter=1.37)
    out = []
    for n in (1, 2, 4):
        chunks = [ts_all[i::n] for i in range(n)]
        # S2-tuned: n threads each running ffmpeg processes back-to-back
        b.srv.reset(**PROFILES["cdn"])
        lat = []
        cpu = []
        t0 = time.perf_counter()

        def worker(ch, k):
            res = []
            for j, t in enumerate(ch):
                r = extract.ffmpeg_frame(b.url(fx), t, FRAMES / f"conc_{n}_{k}_{j}.jpg",
                                         **FF_STRATEGIES["S2-tuned-slice"])
                res.append((r.proc.wall_s, r.proc.cpu_s or 0, r.ok))
            return res

        with cf.ThreadPoolExecutor(n) as pool:
            for res in pool.map(worker, chunks, range(n)):
                lat += [x[0] for x in res]
                cpu += [x[1] for x in res]
        wall = time.perf_counter() - t0
        st = b.srv.stats()
        r = {"strategy": "S2-tuned-slice", "parallel": n, "frames": 16, "wall_s": round(wall, 3),
             "frames_per_s": round(16 / wall, 3), "latency_mean_s": round(statistics.mean(lat), 3),
             "latency_p95_s": pct(lat, 95), "cpu_s_per_frame": round(sum(cpu) / 16, 4),
             "bytes_per_frame": round(st["bytes"] / 16)}
        log(f"[concurrency] {r}")
        out.append(r)
        # S5: n worker processes, each with its own persistent container
        b.srv.reset(**PROFILES["cdn"])
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(n) as pool:
            recs = list(pool.map(lambda ch: b.s5(fx, ch, "cdn", reset=False, verify=False), chunks))
        wall = time.perf_counter() - t0
        st = b.srv.stats()
        lat = [f["wall_s"] for rr in recs for f in rr["frames"]]
        r = {"strategy": "S5-keyframe", "parallel": n, "frames": 16, "wall_s": round(wall, 3),
             "frames_per_s": round(16 / wall, 3), "latency_mean_s": round(statistics.mean(lat), 3),
             "latency_p95_s": pct(lat, 95), "open_s_mean": round(statistics.mean(rr["open_s"] or 0 for rr in recs), 3),
             "cpu_s_per_frame": round(sum((rr["cpu_s"] or 0) for rr in recs) / 16, 4),
             "bytes_per_frame": round(st["bytes"] / 16), "ok": sum(rr["ok"] for rr in recs)}
        log(f"[concurrency] {r}")
        out.append(r)
        RESULTS["concurrency"] = out
        save()


PLAYBACK_JOBS = ["none", "S5x1", "S5x2", "S5x4", "S2x1", "S2x2", "S2x4"]
PLAYBACK_RATES = [28.7, 60.0]


def exp_playback(b: Bench):
    """Does thumbnail extraction starve a player sharing the same 100 Mbit/s link?"""
    fx = "remux_like.mkv"
    dur = MANIFEST[fx]["duration"]
    out = []
    for play_mbps in PLAYBACK_RATES:
        for job in PLAYBACK_JOBS:
            b.srv.reset(**PROFILES["cdn"])
            pb = Playback(b.srv.base, f"/f/{fx}", play_mbps * 1e6, offset=5_000_000_000, duration_s=25)
            ts = spread(dur, 16, jitter=2.37)
            job_window = [None, None]

            def run_job():
                time.sleep(0.3)  # the user scrubbed: player rebuffers while previews are generated
                job_window[0] = time.perf_counter() - t_start
                k = int(job[3:])
                if job.startswith("S5"):
                    with cf.ThreadPoolExecutor(k) as pool:
                        list(pool.map(lambda ch: b.s5(fx, ch, "cdn", reset=False, verify=False),
                                      [ts[i::k] for i in range(k)]))
                else:
                    def w(ch, kk):
                        for j, t in enumerate(ch):
                            extract.ffmpeg_frame(b.url(fx), t, FRAMES / f"pb_{kk}_{j}.jpg",
                                                 **FF_STRATEGIES["S2-tuned-slice"])
                    with cf.ThreadPoolExecutor(k) as pool:
                        list(pool.map(w, [ts[i::k] for i in range(k)], range(k)))
                job_window[1] = time.perf_counter() - t_start

            t_start = time.perf_counter()
            pb.start()
            jt = None
            if job != "none":
                jt = threading.Thread(target=run_job)
                jt.start()
                jt.join()
            pb.join()
            st = b.srv.stats(wait=0.5)
            a, z = (job_window if job != "none" else (0.3, 10.0))
            thr = pb.throughput(a or 0.3, z or 10.0)
            r = {"playback_mbps": play_mbps, "job": job, "job_window_s": [round(x, 2) if x else x for x in job_window],
                 "startup_s": pb.startup_s, "stalls": pb.stalls, "stall_s": round(pb.stall_s, 2),
                 "playback_mbps_during_job": round(thr / 1e6, 1) if thr else None,
                 "playback_ok": (pb.stalls == 0 and (pb.startup_s or 99) < 3.0),
                 "thumb_bytes": sum(v["bytes"] for k, v in st["by_ua"].items() if k != "playback"),
                 "playback_bytes": st["by_ua"].get("playback", {}).get("bytes")}
            log(f"[playback] {r}")
            out.append(r)
            RESULTS["playback"] = out
            save()


def _warm_cache_fetch(paths, profile: str) -> dict:
    """warm-cache = thumbnail already generated: time to serve the stored JPEG over HTTP
    (same throttled profile, one keep-alive connection, server rooted at the thumbnail dir)."""
    paths = [Path(p) for p in paths if p and Path(p).exists()]
    if not paths:
        return {"n": 0}
    s = BenchServer(paths[0].parent).start()
    try:
        s.reset(**PROFILES[profile])
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
        lat = []
        for p in paths:
            if p.parent != paths[0].parent:
                continue
            t0 = time.perf_counter()
            c.request("GET", f"/f/{p.name}")
            body = c.getresponse().read()
            lat.append(time.perf_counter() - t0)
            assert len(body) == p.stat().st_size
        c.close()
        st = s.stats()
    finally:
        s.stop()
    return {"n": len(lat), "first_s": round(lat[0], 4), "mean_s": round(statistics.mean(lat), 4),
            "mean_s_excl_first": round(statistics.mean(lat[1:]), 4) if len(lat) > 1 else None,
            "bytes_per_thumb": round(st["bytes"] / len(lat)), "requests": st["requests"]}


SCRUB_T = 4800.37  # 80:00 (+0.37 s so it does not land on a keyframe)
SCRUB_NEIGH = [4750.37 + 10 * i for i in range(11) if i != 5]  # 79:10 .. 80:50, 10 s apart
SCRUB_FAR = 600.37  # far jump on an already-open source


def exp_scrub(b: Bench):
    """ON-DEMAND scenario: user scrubs to 80:00 on remux_like.mkv (cdn profile).

    Request order: T=80:00 first, then 10 neighbours, then one far jump (10:00).
      cold         = new job: nothing probed/opened (includes preflight/probe, header, Cues read)
      warm-source  = same source already probed/open (persistent container / cached probe), new timestamp
      warm-cache   = thumbnail already generated, served from disk over HTTP
    """
    fx = "remux_like.mkv"
    T, neigh, far = SCRUB_T, SCRUB_NEIGH, SCRUB_FAR
    ts = [T] + neigh + [far]
    out = []
    # --- S5: one persistent PyAV container (production candidate)
    for acc in (False, True):
        r = b.s5(fx, ts, "cdn", accurate=acc)
        fr = r["frames"]
        warm = [f for f in fr[1:11] if f["ok"]]
        rr = {"strategy": r["strategy"], "parallel": 1,
              "cold_first_s": r["cold_first_s"],
              "cold_open_s": r["open_s"],
              "cold_bytes": ((r.get("open_bytes") or 0) + (fr[0].get("bytes") or 0)) if fr else None,
              "cold_requests": ((r.get("open_requests") or 0) + (fr[0].get("requests") or 0)) if fr else None,
              "warm_source_mean_s": round(statistics.mean(f["wall_s"] for f in warm), 4) if warm else None,
              "warm_source_p95_s": pct([f["wall_s"] for f in warm], 95),
              "warm_source_bytes_per_frame": round(statistics.mean(f["bytes"] for f in warm)) if warm else None,
              "warm_source_requests_per_frame": round(statistics.mean(f["requests"] for f in warm), 2) if warm else None,
              "far_jump_s": fr[-1]["wall_s"] if len(fr) == len(ts) else None,
              "all_11_s": fr[10]["since_start"] if len(fr) > 10 else None,
              "total_bytes": r.get("server_bytes"), "total_requests": r.get("server_requests"),
              "cpu_s_total": r.get("cpu_s"), "peak_rss": r.get("peak_rss"),
              "accuracy": r["accuracy"], "errors": [f["error"] for f in fr if f["error"]]}
        rr["warm_cache"] = _warm_cache_fetch([f.get("out") for f in fr[:11]], "cdn")
        log(f"[on-demand] {rr}")
        out.append(rr)
    # --- S2/S1 per-process ffmpeg: cold = preflight + probe + first frame; later frames reuse the
    # cached probe, but every process re-reads header + Cues (no persistent demuxer state)
    for strat, par in (("S2-tuned-slice", 1), ("S2-tuned-slice", 4), ("S1-tuned-slice", 1)):
        b.srv.reset(**PROFILES["cdn"])
        t0 = time.perf_counter()
        pf = extract.preflight(b.url(fx))
        pr = extract.probe(b.url(fx), http=FF_STRATEGIES[strat]["http"])
        t_probe = time.perf_counter() - t0
        cold_st = b.srv.stats()
        r0 = extract.ffmpeg_frame(b.url(fx), T, FRAMES / f"od_{strat}_{par}_00.jpg", **FF_STRATEGIES[strat])
        t_first = time.perf_counter() - t0
        st1 = b.srv.stats()
        done, walls = [], []

        def one(t, i):
            r = extract.ffmpeg_frame(b.url(fx), t, FRAMES / f"od_{strat}_{par}_{i + 1:02d}.jpg",
                                     **FF_STRATEGIES[strat])
            done.append(time.perf_counter() - t0)
            walls.append(r.proc.wall_s)
            return check_frame(r.out, t), r.out

        with cf.ThreadPoolExecutor(par) as pool:
            res = list(pool.map(one, neigh, range(len(neigh))))
        st = b.srv.stats()
        checks = [check_frame(r0.out, T)] + [c for c, _ in res]
        rr = {"strategy": strat, "parallel": par, "preflight_probe_s": round(t_probe, 3),
              "cold_first_s": round(t_first, 3),
              "cold_bytes": st1["bytes"], "cold_requests": st1["requests"],
              "probe_bytes": cold_st["bytes"], "probe_requests": cold_st["requests"],
              "warm_source_mean_s": round(statistics.mean(walls), 4),
              "warm_source_p95_s": pct(walls, 95),
              "warm_source_bytes_per_frame": round((st["bytes"] - st1["bytes"]) / len(neigh)),
              "warm_source_requests_per_frame": round((st["requests"] - st1["requests"]) / len(neigh), 2),
              "all_11_s": round(max(done), 3), "total_bytes": st["bytes"], "total_requests": st["requests"],
              "accuracy": acc_summary(checks), "probe_ok": pr.ok, "preflight_ok": pf["ok"],
              "frame0_ok": r0.ok, "errors": sorted({"frame0:" + r0.error} if r0.error else set())}
        rr["warm_cache"] = _warm_cache_fetch([r0.out] + [o for _, o in res], "cdn")
        log(f"[on-demand] {rr}")
        out.append(rr)
    RESULTS["scrub"] = out
    save()


def exp_scrub_seq(b: Bench):
    """Naive alternative: sequential S4 from t=0; time until the 80:00 thumbnail exists."""
    fx = "remux_like.mkv"
    target_t = 4800
    b.srv.reset(**PROFILES["cdn"])
    od = FRAMES / "s4_scrub"
    od.mkdir(parents=True, exist_ok=True)
    for p in od.glob("*.jpg"):
        p.unlink()
    k = target_t // 10 + 1  # fps=1/10 round=down: output k covers t=(k-1)*10
    target_file = od / f"s{k:05d}.jpg"
    after_file = od / f"s{k + 1:05d}.jpg"
    ev = threading.Event()
    res = {}

    def run():
        res["r"] = extract.ffmpeg_sequential(b.url(fx), 10, od, timeout_s=3600, cancel=ev)

    t0 = time.perf_counter()
    th = threading.Thread(target=run)
    th.start()
    t_hit = None
    progress = []
    while th.is_alive():
        time.sleep(0.5)
        if not t_hit and after_file.exists():
            t_hit = time.perf_counter() - t0
            ev.set()
        if int(time.perf_counter() - t0) % 60 == 0:
            n = len(list(od.glob("*.jpg")))
            if not progress or progress[-1][1] != n:
                progress.append((round(time.perf_counter() - t0), n))
                log(f"[scrub_seq] {progress[-1][0]}s: {n} thumbs (~{(n - 1) * 10}s of media)")
    th.join()
    st = b.srv.stats()
    r = {"strategy": "S4-sequential-from-0", "target_t": target_t, "time_to_target_s": round(t_hit, 1) if t_hit else None,
         "bytes_at_target": st["bytes"], "check": check_frame(str(target_file), float(target_t)),
         "progress": progress}
    log(f"[scrub_seq] {r}")
    RESULTS["scrub_seq"] = r
    save()


# ---------------------------------------------------------------- real remote sources
INTERNET_SOURCES = [
    ("blender_sintel_1080p_mkv", "https://download.blender.org/durian/movies/Sintel.2010.1080p.mkv"),
    ("jellyfin_4k_hevc_hdr10_60M_mp4",
     "https://repo.jellyfin.org/test-videos/HDR/HDR10/HEVC/Test%20Jellyfin%204K%20HEVC%20HDR10%2060M.mp4"),
]
INTERNET_BUDGET = 1.8e9


def remote_suite(b: Bench, name: str, url: str, budget: float, host_only: bool) -> dict:
    """Preflight, probe, then 1 -> 5 frames with S2-tuned / S1-tuned (via the accounting proxy)
    and S5 (direct, client-side byte count). Stops if the byte budget is reached."""
    rec = {"source": redact_url(url, host_only=host_only), "runs": []}
    net0 = psutil.net_io_counters().bytes_recv
    used = 0
    pf = extract.preflight(url, timeout_s=20)
    rec["preflight"] = pf
    log(f"[{name}] preflight {pf}")
    if not pf["ok"]:
        rec["blocked"] = f"preflight failed: {pf['error']}"
        return rec
    b.srv.state.upstream_url = url
    purl = f"{b.srv.base}/u/source"
    b.srv.reset()
    pr = extract.probe(purl, http=FF_STRATEGIES["S2-tuned-slice"]["http"], timeout_s=60)
    st = b.srv.stats()
    rec["probe"] = {"ok": pr.ok, "error": pr.error, "duration": pr.duration, "vcodec": pr.vcodec,
                    "w": pr.width, "h": pr.height, "pix_fmt": pr.pix_fmt, "trc": pr.color_transfer,
                    "hdr": pr.hdr, "s": round(pr.proc.wall_s, 3), "bytes": st["bytes"], "requests": st["requests"]}
    used += st["bytes"]
    log(f"[{name}] probe {rec['probe']}")
    if not pr.ok or not pr.duration:
        rec["blocked"] = "probe failed"
        return rec
    dur = pr.duration
    for n in (1, 5):
        ts = spread(dur, n, jitter=0.0) if n > 1 else [round(dur * 0.8, 3)]
        for strat in ("S2-tuned-slice", "S1-tuned-slice"):
            if used > budget:
                break
            r = b.ff_frames("remote", ts, strat, "lan", url=purl, hdr=pr.hdr, verify=False, keep_log=(n == 1),
                            timeout_s=120)
            r["fixture"] = name
            used += r["server_bytes"]
            r["file_bytes"] = pf["size"]
            log(f"[{name}] n={n} {strat} first={r['cold_first_s']}s mean={r['frame_s_mean']}s "
                f"{r['bytes_per_frame'] / 1e6:.2f}MB/f req/f={r['requests_per_frame']} cpu/f={r['cpu_s_per_frame']} "
                f"err={r['errors']}")
            rec["runs"].append(r)
        if used > budget:
            break
        for acc in (False, True):
            r = b.s5(name, ts, "lan", url=url, accurate=acc, verify=False, reset=False, timeout_s=300)
            got = sum((f["bytes"] or 0) for f in r["frames"]) + (r["open_bytes"] or 0)
            used += got
            r.update(client_bytes=got, bytes_per_frame=round(got / max(1, r["ok"])),
                     requests_per_frame=round((sum((f["requests"] or 0) for f in r["frames"]) +
                                               (r["open_requests"] or 0)) / max(1, r["ok"]), 2),
                     cpu_s_per_frame=round((r["cpu_s"] or 0) / max(1, r["ok"]), 4), file_bytes=pf["size"])
            log(f"[{name}] n={n} {r['strategy']} open={r['open_s']}s first={r['cold_first_s']}s "
                f"warm={r['warm_frame_s_mean']}s {r['bytes_per_frame'] / 1e6:.2f}MB/f "
                f"pts={[f.get('pts') for f in r['frames']]} err={r['error']}")
            rec["runs"].append(r)
    b.srv.state.upstream_url = None
    rec["bytes_used_accounted"] = used
    rec["net_io_recv_delta"] = psutil.net_io_counters().bytes_recv - net0
    return rec


def exp_internet(b: Bench):
    out = {}
    used = 0
    for name, url in INTERNET_SOURCES:
        rec = remote_suite(b, name, url, INTERNET_BUDGET - used, host_only=False)
        used += rec.get("bytes_used_accounted", 0)
        out[name] = rec
        RESULTS["internet"] = out
        save()


def exp_torbox(b: Bench, env_name: str):
    url = os.environ.get(env_name or "", "")
    if not url:
        RESULTS["torbox"] = {"blocked": f"env var {env_name or 'OPENSEEK_BENCH_URL'} not set",
                             "how": "set OPENSEEK_BENCH_URL to a TorBox CDN link, then: "
                                    "python run_bench.py --exp torbox --url-env OPENSEEK_BENCH_URL"}
        log("[torbox] BLOCKED: no URL")
        save()
        return
    RESULTS["torbox"] = remote_suite(b, "torbox", url, 3e9, host_only=True)
    save()


EXPERIMENTS = {
    "env": exp_env, "containers": exp_containers, "strategies": exp_strategies, "gop10": exp_gop10,
    "hdr": exp_hdr, "sequential": exp_sequential, "failures": exp_failures, "concurrency": exp_concurrency,
    "playback": exp_playback, "scrub": exp_scrub, "scrub_seq": exp_scrub_seq, "internet": exp_internet,
}
DEFAULT = ["env", "containers", "strategies", "gop10", "hdr", "sequential", "failures", "concurrency",
           "playback", "scrub", "scrub_seq"]
QUICK_EXPS = ["env", "containers", "scrub", "playback", "failures"]
QUICK = False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default=",".join(DEFAULT))
    ap.add_argument("--url-env", default="OPENSEEK_BENCH_URL")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="~5 min subset (on-demand 80:00 cold/warm, playback impact at 1/2/4, containers, "
                         "failures) written to results/quick/")
    a = ap.parse_args()
    global RESULTS, RES, FRAMES, QUICK
    if a.quick:
        QUICK = True
        RES = HERE / "results" / "quick"
        FRAMES = RES / "frames"
        RES.mkdir(parents=True, exist_ok=True)
        if a.exp == ",".join(DEFAULT):
            a.exp = ",".join(QUICK_EXPS)
        PLAYBACK_RATES[:] = [28.7]
        PLAYBACK_JOBS[:] = ["none", "S5x1", "S5x2", "S5x4", "S2x4"]
    if (RES / "results.json").exists() and not a.quick:
        RESULTS = json.loads((RES / "results.json").read_text())
    RESULTS["mode"] = "quick" if a.quick else "full"
    if not a.report_only:
        b = Bench()
        for e in a.exp.split(","):
            e = e.strip()
            log(f"===== {e}")
            t0 = time.time()
            if e == "torbox":
                exp_torbox(b, a.url_env)
            else:
                EXPERIMENTS[e](b)
            RESULTS.setdefault("timings", {})[e] = round(time.time() - t0, 1)
            save()
        b.srv.stop()
    import report

    report.write(RESULTS, RES / "REPORT.md")
    log(f"wrote {RES / 'REPORT.md'}")


if __name__ == "__main__":
    main()
