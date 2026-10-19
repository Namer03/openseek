"""Generate synthetic benchmark fixtures with ffmpeg (no copyrighted content).

Every fixture carries the frame-number barcode from barcode.py so extracted thumbnails can be
checked against the requested timestamp without OCR.

Usage:  python fixtures.py [--only name,name] [--force] [--list]

Fixtures (sizes are approximate targets; actual sizes are printed and written to fixtures/manifest.json):
  remux_like.mkv     1080p24 H.264 ~28 Mbit/s + AC3 640k, GOP 2 s, 82 min, Cues at END (ffmpeg default:
                     SeekHead at front, Cues + Tags after the last Cluster, like mkvmerge)   ~17.5 GB
  med_cues_end.mkv   first 4 min of remux_like, stream copy, Cues at end                     ~0.85 GB
  med_cues_front.mkv same, muxed with -cues_to_front 1 (Cues before the Clusters)
  med_no_cues.mkv    same as med_cues_end, with the Cues element overwritten in place by an EBML Void
                     element (mkvmerge --no-cues is not installed; this isolates "index missing")
  med_moov_end.mp4   same 4 min, MP4, moov atom at END (ffmpeg default)
  med_faststart.mp4  same 4 min, MP4, -movflags +faststart (moov at front)
  gop10.mkv          1080p24 H.264 ~28 Mbit/s, GOP 10 s (240 frames), 6 min, Cues at end      ~1.3 GB
  hdr4k.mkv          2160p24 HEVC Main10, HDR10 (bt2020 / smpte2084 + mastering display + CLL), 60 s
  small.mkv          1080p24 H.264 ~8 Mbit/s, GOP 2 s, 60 s (unit tests, full-decode S4)
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import barcode

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
FPS = barcode.FPS


def run(args: list[str]) -> None:
    print("  $", " ".join(a if " " not in a else repr(a) for a in args[:6]), "...", flush=True)
    t0 = time.time()
    subprocess.run(args, check=True)
    print(f"  done in {time.time() - t0:.1f}s", flush=True)


def synth_video_inputs(w: int, h: int, noise: int) -> tuple[list[str], str]:
    """Inputs + filtergraph producing a noisy testsrc2 background with the barcode on top."""
    inputs = [
        "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={FPS}",
        "-f", "lavfi", "-i", barcode.barcode_source(w, h),
    ]
    graph = (
        f"[0:v]noise=alls={noise}:allf=t,format=yuv420p[bg];"
        f"[1:v]format=yuv420p[bc];[bg][bc]overlay=0:0:shortest=1,format=yuv420p[v]"
    )
    return inputs, graph


def enc_h264(out: Path, w: int, h: int, minutes: float, mbps: float, gop: int, noise: int, audio: bool) -> None:
    inputs, graph = synth_video_inputs(w, h, noise)
    frames = int(minutes * 60 * FPS)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y", *inputs]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-filter_complex", graph, "-map", "[v]"]
    if audio:
        args += ["-map", "2:a", "-c:a", "ac3", "-b:a", "640k", "-ac", "2"]
    args += [
        "-frames:v", str(frames), "-t", str(minutes * 60),
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", f"{mbps}M",
        "-maxrate", f"{mbps * 1.2:.1f}M", "-bufsize", f"{mbps * 2:.0f}M",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        str(out),
    ]
    run(args)


def enc_hdr4k(out: Path, seconds: int) -> None:
    w, h = 3840, 2160
    inputs, graph = synth_video_inputs(w, h, 20)
    graph = graph.replace(
        "format=yuv420p[v]",
        "format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc[v]",
    )
    x265 = (
        "hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
        "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):"
        "max-cll=1000,400:keyint=48:min-keyint=48:scenecut=0:log-level=error"
    )
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y", *inputs,
        "-filter_complex", graph, "-map", "[v]", "-frames:v", str(seconds * FPS),
        "-c:v", "libx265", "-preset", "ultrafast", "-b:v", "40M", "-pix_fmt", "yuv420p10le",
        "-x265-params", x265,
        "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
        str(out),
    ])


def copy_cut(src: Path, out: Path, seconds: int, extra: list[str]) -> None:
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0",
         "-t", str(seconds), "-c", "copy", *extra, str(out)])


# ---------------------------------------------------------------- EBML: replace Cues with Void
CUES_ID = 0x1C53BB6B
SEGMENT_ID = 0x18538067


def _read_vint(f, keep_marker: bool) -> tuple[int, int]:
    first = f.read(1)
    if not first:
        raise EOFError
    b0 = first[0]
    length = 1
    mask = 0x80
    while length <= 8 and not (b0 & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("bad vint")
    rest = f.read(length - 1)
    val = b0 if keep_marker else (b0 & (mask - 1))
    for c in rest:
        val = (val << 8) | c
    return val, length


def void_out_cues(path: Path) -> int:
    """Overwrite the top-level Cues element(s) in place with an EBML Void element. Returns count."""
    n = 0
    with open(path, "r+b") as f:
        eid, _ = _read_vint(f, True)  # EBML header
        size, _ = _read_vint(f, False)
        f.seek(size, 1)
        eid, _ = _read_vint(f, True)
        if eid != SEGMENT_ID:
            raise ValueError("no Segment")
        seg_size, sl = _read_vint(f, False)
        seg_start = f.tell()
        unknown = seg_size == (1 << (7 * sl)) - 1
        end = os.path.getsize(path) if unknown else seg_start + seg_size
        while f.tell() < end:
            pos = f.tell()
            try:
                eid, il = _read_vint(f, True)
                size, szl = _read_vint(f, False)
            except EOFError:
                break
            body = f.tell()
            if eid == CUES_ID:
                total = (body - pos) + size
                if total < 9:
                    raise ValueError("Cues too small to void")
                f.seek(pos)
                # Void ID 0xEC + 8-byte size vint (0x01 marker) covering the rest
                f.write(bytes([0xEC]) + bytes([0x01]) + struct.pack(">Q", total - 9)[1:])
                n += 1
            f.seek(body + size)
    return n


# ---------------------------------------------------------------- driver
def fixtures_spec():
    remux = FIX / "remux_like.mkv"
    med = FIX / "med_cues_end.mkv"
    return {
        "small.mkv": lambda o: enc_h264(o, 1920, 1080, 1, 8, 48, 10, False),
        "remux_like.mkv": lambda o: enc_h264(o, 1920, 1080, 82, 28, 48, 30, True),
        "med_cues_end.mkv": lambda o: copy_cut(remux, o, 240, []),
        "med_cues_front.mkv": lambda o: copy_cut(remux, o, 240, ["-cues_to_front", "1"]),
        "med_no_cues.mkv": lambda o: (copy_cut(med, o, 240, []), print("  voided cues:", void_out_cues(o))),
        "med_moov_end.mp4": lambda o: copy_cut(remux, o, 240, []),
        "med_faststart.mp4": lambda o: copy_cut(remux, o, 240, ["-movflags", "+faststart"]),
        "gop10.mkv": lambda o: enc_h264(o, 1920, 1080, 6, 28, 240, 30, False),
        "hdr4k.mkv": lambda o: enc_hdr4k(o, 60),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    FIX.mkdir(exist_ok=True)
    spec = fixtures_spec()
    only = [x for x in a.only.split(",") if x]
    manifest_path = FIX / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for name, fn in spec.items():
        if only and name not in only:
            continue
        out = FIX / name
        if out.exists() and not a.force:
            print(f"[skip] {name} exists ({out.stat().st_size / 1e9:.2f} GB)")
        elif not a.list:
            print(f"[make] {name}")
            fn(out)
        if out.exists():
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,bit_rate",
                                 "-of", "json", str(out)], capture_output=True, text=True, check=True)
            fmt = json.loads(pr.stdout)["format"]
            manifest[name] = {"bytes": out.stat().st_size, "duration": float(fmt["duration"]),
                              "bit_rate": int(fmt.get("bit_rate", 0))}
            print(f"  {name}: {out.stat().st_size / 1e9:.3f} GB, {float(fmt['duration']):.1f}s, "
                  f"{int(fmt.get('bit_rate', 0)) / 1e6:.1f} Mbit/s")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    total = sum(p.stat().st_size for p in FIX.glob("*") if p.is_file())
    print(f"fixtures total: {total / 1e9:.2f} GB")
    if total > 25e9:
        print("WARNING: fixtures exceed 25 GB budget", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
