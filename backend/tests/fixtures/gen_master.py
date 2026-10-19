"""ONE deterministic master per 'film'; every positive test variant is a controlled transformation of that exact
master (so frames genuinely correspond). Negatives are separate masters. Usage: gen_master.py [seconds=120] [only=play,ok,...] [--out DIR]   (needs ffmpeg on PATH)

  master_A.mkv   1280x720 24 fps, near-lossless, grain + frame-number barcode (ground truth of every frame)
  master_B.mkv   a different film (negative)
  play_*.mkv     master_A -> 1920x1080 ~6 Mbit/s GOP 2 s            (stand-in for the 4K playback source)
  ok_*.mkv       master_A -> 1280x720 ~5 Mbit/s GOP 4 s            (identical timeline)
  off_*.mkv      7 s of master_B prepended, then ok                (constant offset +7000 ms)
  pal_*.mkv      master_A sped up 25/24 at 25 fps                   (speed 0.96)
  ins_*.mkv      master_A[0,S/2) + 10 s of master_B + master_A[S/2,S)   (inserted scene)
  swap_*.mkv     master_A[S/2,S) + master_A[0,S/2)                  (nonlinear: halves swapped)
  other_*.mkv    master_B -> 720p                                    (different film)
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "bench"))   # repo's bench/barcode.py
import barcode  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("OPENSEEK_TEST_FIXTURES") or Path(__file__).resolve().parent / "generated")

OUT = DEFAULT_OUT
S = 120
ONLY = None
CRF = "14"
SUF = "120s"
FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


def run(args):
    subprocess.run(args, check=True)


def palette(seed: int) -> str:
    return ":".join(f"c{i}=0x{(seed * 53 + i * 97) % 200 + 40:02x}{(seed * 29 + i * 71) % 200 + 40:02x}"
                    f"{(seed * 17 + i * 131) % 200 + 40:02x}" for i in range(4))


def master(seed: int, out: Path) -> None:
    g = f"gradients=s=640x360:r=2:speed=0.07:nb_colors=4:{palette(seed)}:seed={seed}:duration={S}"
    run(FF + ["-f", "lavfi", "-i", g, "-f", "lavfi", "-i", barcode.barcode_source(1280, 720, 24),
              "-filter_complex", f"[0:v]fps=24,scale=1280:720:flags=bicubic,format=yuv420p,"
                                 f"geq=lum='lum(X,Y)+34*sin(X/41+T*0.6+{seed % 7})*cos(Y/29-T*0.45+{seed % 5})"
                                 f"+22*sin((X+Y)/53+T*0.3+{seed % 3})':cb='cb(X,Y)':cr='cr(X,Y)',"
                                 f"noise=alls=10:allf=t,format=yuv420p[bg];"
                                 "[bg][1:v]overlay=0:0:shortest=1,format=yuv420p[v]",
              "-map", "[v]", "-t", str(S), "-c:v", "libx264", "-preset", "fast", "-crf", CRF, "-g", "48",
              "-r", "24", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", str(out)])


def enc(extra_in, vf, out, w, h, mbps, gop, fps=24, filter_complex=None):
    args = FF + extra_in
    if filter_complex:
        args += ["-filter_complex", filter_complex, "-map", "[v]"]
    else:
        args += ["-vf", vf]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", f"{mbps}M", "-maxrate", f"{mbps * 1.3:.1f}M",
             "-bufsize", f"{mbps * 2:.0f}M", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
             "-r", str(fps), "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", str(out)]
    run(args)


def want(n: str) -> bool:
    return ONLY is None or n in ONLY


def generate(seconds: int, only: set | None, out: Path) -> None:
    global S, ONLY, CRF, SUF, OUT
    S, ONLY, OUT = seconds, only, Path(out)
    CRF = "14" if S <= 900 else "24"            # long masters stay a manageable size
    SUF = f"{S}s"
    OUT.mkdir(parents=True, exist_ok=True)
    main()


def main() -> None:
    A, B = OUT / f"master_A_{SUF}.mkv", OUT / f"master_B_{SUF}.mkv"
    master(7, A)
    if ONLY is None or ONLY & {"off", "ins", "other"}:
        master(99, B)
    i, h = ["-i", str(A)], S // 2
    if want("play"):
        enc(i, "scale=1920:1080:flags=bicubic,format=yuv420p", OUT / f"play_{SUF}.mkv", 1920, 1080, 6, 48)
    if want("ok"):
        enc(i, "scale=1280:720:flags=bicubic,format=yuv420p", OUT / f"ok_{SUF}.mkv", 1280, 720, 5, 96)
    if want("pal"):
        enc(i, "setpts=PTS*24/25,fps=25,scale=1280:720,format=yuv420p", OUT / f"pal_{SUF}.mkv", 1280, 720, 5, 96, 25)
    if want("off"):
        enc(["-i", str(B), "-i", str(A)], "", OUT / f"off_{SUF}.mkv", 1280, 720, 5, 96,
            filter_complex="[0:v]trim=0:7,setpts=PTS-STARTPTS[x];[1:v]setpts=PTS-STARTPTS[m];[x][m]concat=n=2:v=1[c];[c]scale=1280:720,format=yuv420p[v]")
    if want("ins"):
        enc(["-i", str(A), "-i", str(B), "-i", str(A)], "", OUT / f"ins_{SUF}.mkv", 1280, 720, 5, 96,
            filter_complex=f"[0:v]trim=0:{h},setpts=PTS-STARTPTS[a];[1:v]trim=0:10,setpts=PTS-STARTPTS[b];"
                           f"[2:v]trim={h}:{S},setpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=1[x];[x]scale=1280:720,format=yuv420p[v]")
    if want("swap"):
        enc(["-i", str(A), "-i", str(A)], "", OUT / f"swap_{SUF}.mkv", 1280, 720, 5, 96,
            filter_complex=f"[0:v]trim={h}:{S},setpts=PTS-STARTPTS[a];[1:v]trim=0:{h},setpts=PTS-STARTPTS[b];"
                           f"[a][b]concat=n=2:v=1[x];[x]scale=1280:720,format=yuv420p[v]")
    if want("other"):
        enc(["-i", str(B)], "scale=1280:720:flags=bicubic,format=yuv420p", OUT / f"other_{SUF}.mkv", 1280, 720, 5, 96)
    for p in sorted(OUT.glob(f"*_{SUF}.mkv")):
        print("built", p.name, round(p.stat().st_size / 1e6, 1), "MB")


if __name__ == "__main__":
    argv = sys.argv[1:]
    out = DEFAULT_OUT
    if "--out" in argv:
        i = argv.index("--out")
        out = Path(argv[i + 1]).resolve()
        del argv[i:i + 2]
    generate(int(argv[0]) if argv else 120, set(argv[1].split(",")) if len(argv) > 1 else None, out)
