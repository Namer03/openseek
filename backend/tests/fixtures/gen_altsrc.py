"""Paired fixtures for the alternative-source pipeline (synthetic, reproducible; needs ffmpeg on PATH).

Usage: python gen_altsrc.py [10min|2h|all] [--out DIR]   (default: 10min set into $OPENSEEK_TEST_FIXTURES or ./generated;
the 10min run also builds the gen_master.py 120 s and 600 s sets used by test_alt_e2e; --no-master skips them).

Content = slowly evolving 'gradients' (unique over hours, corr 0.85 at 5 s lag, ~0 at 30 s) + grain so the
bitrate is realistic, + the frame-number barcode on top (OCR-free ground truth of each frame's own timeline).
  play_*  : 1920x1080, ~6 Mbit/s, 2 s GOP  (stand-in for the 4K playback source)
  ok_*    : 1280x720,  ~5 Mbit/s, 4 s GOP  (same cut, same timeline)
  off_*   : ok + 7 s of unrelated intro      (constant offset +7000 ms)
  pal_*   : ok sped up 25/24 at 25 fps       (speed 0.96)
  other_* : a different film, same length    (must be rejected)
  ins_*   : ok with 90 s of other material inserted at 300 s (must be rejected)
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "bench"))   # repo's bench/barcode.py
import barcode  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("OPENSEEK_TEST_FIXTURES") or Path(__file__).resolve().parent / "generated")

OUT = DEFAULT_OUT


def seg(seed: int, seconds: float, w: int, h: int, fps: int = 24, grain: int = 12, speed: float = 0.07) -> list[str]:
    # content is rendered at a FIXED 640x360 and scaled per version: gradients draws different patterns at
    # different sizes, which would make "the same film" differ between the 1080p and 720p files
    # explicit palette: with only `seed` the colours differed between runs (luma inverted between encodes)
    cols = ":".join(f"c{i}=0x{(seed * 53 + i * 97 + j * 61) % 200 + 40:02x}{(seed * 29 + i * 71 + j * 43) % 200 + 40:02x}"
                    f"{(seed * 17 + i * 131 + j * 83) % 200 + 40:02x}" for i, j in zip(range(4), range(4)))
    return ["-f", "lavfi", "-i",
            f"gradients=s=640x360:r=2:speed={speed}:nb_colors=4:{cols}:seed={seed}:duration={seconds}"]


def build(out: Path, w: int, h: int, mbps: float, gop: int, segments: list[tuple[int, float]], fps: int = 24,
          stretch: float = 1.0, grain: int = 12) -> None:
    inputs, chains = [], []
    for i, (seed, secs) in enumerate(segments):
        inputs += seg(seed, secs, w, h)
        chains.append(f"[{i}:v]fps={fps},scale={w}:{h}:flags=bicubic[s{i}]")
    n = len(segments)
    cat = "".join(f"[s{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[c]" if n > 1 else "[s0]null[c]"
    inputs += ["-f", "lavfi", "-i", barcode.barcode_source(w, h, fps)]
    graph = ";".join(chains + [cat,
        f"[c]setpts=PTS*{stretch},noise=alls={grain}:allf=t,format=yuv420p[bg]",
        f"[bg][{n}:v]overlay=0:0:shortest=1,format=yuv420p[v]"])
    total = sum(s for _, s in segments) * stretch
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]",
            "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", f"{mbps}M",
            "-maxrate", f"{mbps * 1.3:.1f}M", "-bufsize", f"{mbps * 2:.0f}M", "-g", str(gop), "-keyint_min", str(gop),
            "-sc_threshold", "0", "-r", str(fps), "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", str(out)]
    subprocess.run(args, check=True)
    print("built", out.name, round(out.stat().st_size / 1e6), "MB")


def ten_min() -> None:
    S = 600
    build(OUT / "play_10min.mkv", 1920, 1080, 6, 48, [(7, S)], grain=8)
    build(OUT / "ok_10min.mkv", 1280, 720, 5, 96, [(7, S)])
    build(OUT / "off_10min.mkv", 1280, 720, 5, 96, [(11, 7), (7, S)])
    build(OUT / "pal_10min.mkv", 1280, 720, 5, 96, [(7, S)], fps=25, stretch=24 / 25)
    build(OUT / "other_10min.mkv", 1280, 720, 5, 96, [(99, S)])
    build(OUT / "ins_10min.mkv", 1280, 720, 5, 96, [(7, 300), (55, 90), (7, S - 300)])


def two_h() -> None:
    S = 7200
    build(OUT / "play_2h.mkv", 1920, 1080, 6, 48, [(7, S)], grain=8)
    build(OUT / "ok_2h.mkv", 1280, 720, 5, 96, [(7, S)])


def main(argv: list[str]) -> None:
    global OUT
    args = list(argv)
    if "--out" in args:
        i = args.index("--out")
        OUT = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    no_master = "--no-master" in args
    args = [a for a in args if a != "--no-master"]
    what = args[0] if args else "10min"
    OUT.mkdir(parents=True, exist_ok=True)
    if what in ("10min", "all"):
        ten_min()
        if not no_master:
            import gen_master
            for secs in (120, 600):
                gen_master.generate(secs, None, OUT)
    if what in ("2h", "all"):
        two_h()


if __name__ == "__main__":
    main(sys.argv[1:])
