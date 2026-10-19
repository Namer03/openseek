"""2-hour pair WITHOUT regenerating content: loop the verified 10-minute master 12x, vary the crop window per repeat
(period 6) so repeats do not look alike, burn ONE absolute frame-number barcode across the 2 hours (ground truth), and
encode both versions from the SAME decoded frames in one ffmpeg pass (so they genuinely correspond):
  play60_2h.mkv  1920x1080 ~60 Mbit/s GOP 2 s  (remux-weight playback source stand-in)
  ok_2h.mkv      1280x720  ~5  Mbit/s GOP 4 s  (matching thumbnail source)
Needs master_A_600s.mkv in the output dir (python gen_master.py 600) and ffmpeg; writes ~GBs.
Absolute barcode N = frame index since 0 -> tA = N/24 s (18 bits: up to 182 min)."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "bench"))   # repo's bench/barcode.py
import barcode  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("OPENSEEK_TEST_FIXTURES") or Path(__file__).resolve().parent / "generated")

D = DEFAULT_OUT
if "--out" in sys.argv:
    D = Path(sys.argv[sys.argv.index("--out") + 1]).resolve()
SEC = 7200
M = D / "master_A_600s.mkv"
fc = (
    "[0:v]crop=in_w*0.92:in_h*0.92:x='(in_w-out_w)*mod(floor(t/600),6)/5':"
    "y='(in_h-out_h)*mod(floor(t/600)*5,6)/5',scale=1280:720:flags=bicubic,format=yuv420p,split=2[p][q];"
    "[p]scale=1920:1080:flags=bicubic[ph];"
    "[ph][1:v]overlay=0:0:shortest=1,format=yuv420p[play];"
    "[q][2:v]overlay=0:0:shortest=1,format=yuv420p[ok]"
)
cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
       "-stream_loop", str(SEC // 600 - 1), "-i", str(M),
       "-f", "lavfi", "-i", barcode.barcode_source(1920, 1080, 24),
       "-f", "lavfi", "-i", barcode.barcode_source(1280, 720, 24),
       "-filter_complex", fc,
       "-map", "[play]", "-t", str(SEC), "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "60M",
       "-maxrate", "78M", "-bufsize", "120M", "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
       "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", str(D / "play60_2h.mkv"),
       "-map", "[ok]", "-t", str(SEC), "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "5M",
       "-maxrate", "6.5M", "-bufsize", "10M", "-g", "96", "-keyint_min", "96", "-sc_threshold", "0",
       "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", str(D / "ok_2h.mkv")]
subprocess.run(cmd, check=True)
for n in ("play60_2h.mkv", "ok_2h.mkv"):
    print("built", n, round((D / n).stat().st_size / 1e9, 2), "GB")
