import subprocess
import threading
import time

import psutil
import pytest

import barcode
import extract
from redact import redact_text, redact_url

BLOB = bytes(range(256)) * 4096


def test_redact_url_and_text():
    u = "https://cdn.example.com/dld/abc?token=SECRET&exp=123"
    assert redact_url(u) == "https://cdn.example.com/dld/abc?token=REDACTED&exp=REDACTED"
    h = redact_url(u, host_only=True)
    assert "abc" not in h and "SECRET" not in h and h.startswith("https://cdn.example.com/<path#")
    t = f"[http @ 0x1] HTTP error 403 Forbidden\n{u}: Server returned 403"
    assert "SECRET" not in redact_text(t)
    assert "pw" not in redact_url("http://user:pw@h.example/x")


def test_never_uses_shell(monkeypatch, srv, tmp_path):
    seen = []
    real = subprocess.Popen

    def spy(args, *a, **kw):
        seen.append((args, kw.get("shell", False)))
        return real(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    srv.reset()
    extract.ffmpeg_frame(f"{srv.base}/f/tiny.mkv?sig=zzz", 1.0, tmp_path / "x.jpg")
    assert seen and all(isinstance(a, list) and sh is False for a, sh in seen)


def test_run_proc_rejects_string_command():
    with pytest.raises(AssertionError):
        extract.run_proc("ffmpeg -version", 5)


def test_extract_accurate_and_keyframe(srv, tmp_path):
    from PIL import Image

    srv.reset()
    url = f"{srv.base}/f/tiny.mkv"
    r = extract.ffmpeg_frame(url, 3.5, tmp_path / "a.jpg", accurate=True)
    assert r.ok, r.proc.stderr
    with Image.open(r.out) as im:
        assert im.size == (320, 180)
    n, _ = barcode.decode_file(r.out)
    assert abs(n / 24 - 3.5) <= 1 / 24 + 1e-6
    r = extract.ffmpeg_frame(url, 3.5, tmp_path / "k.jpg", accurate=False)
    n, _ = barcode.decode_file(r.out)
    assert n / 24 == 3.0  # GOP 1 s -> snaps back to the keyframe at 3.0
    assert r.proc.cpu_s is not None and r.proc.peak_rss


def test_s5_ranged_reader_bounded_and_single_connection(srv, tmp_path):
    srv.reset()
    ex = extract.AvExtractor(f"{srv.base}/f/tiny.mkv")
    pts = ex.grab(4.6, tmp_path / "s5.jpg", accurate=True)
    assert abs(pts - 4.6) < 1 / 24 + 1e-6
    n, _ = barcode.decode_file(tmp_path / "s5.jpg")
    assert abs(n / 24 - 4.6) < 1 / 24 + 1e-6
    ex.close()
    st = srv.stats()
    assert all(l["range"] and not l["range"].endswith("-") for l in st["log"])  # only bounded ranges
    assert st["connections"] == 1  # one keep-alive connection


def test_range_reader_rejects_no_range_without_draining(srv):
    srv.reset(range=False, rate_mbps=40)
    with pytest.raises(extract.HttpError) as e:
        extract.RangeReader(f"{srv.base}/f/blob.bin")
    assert e.value.category == "no_range_support"
    assert srv.stats()["bytes"] < 600_000


def test_range_reader_redirect_and_retry_after_drop(srv):
    srv.reset(drop_after_bytes=1000, drops=1)
    rr = extract.RangeReader(f"{srv.base}/r/blob.bin", block=4096)
    assert rr.redirects == 1 and rr.size == 1 << 20
    rr.seek(500_000)
    assert rr.read(10) == BLOB[500_000:500_010]
    assert rr.retries == 1


def test_preflight(srv):
    srv.reset()
    assert extract.preflight(f"{srv.base}/f/blob.bin")["range"] is True
    srv.reset(range=False)
    p = extract.preflight(f"{srv.base}/f/blob.bin")
    assert p["ok"] is False and p["error"] == "no_range_support"
    srv.reset()
    assert extract.preflight(f"{srv.base}/f/missing.mkv")["error"] == "http_404"


def test_error_classification_and_stderr_redaction(srv, tmp_path):
    srv.reset()
    r = extract.ffmpeg_frame(f"{srv.base}/f/missing.mkv?token=SECRET", 1, tmp_path / "a.jpg")
    assert not r.ok and r.error == "http_404"
    assert "SECRET" not in r.proc.stderr
    r = extract.ffmpeg_frame(f"{srv.base}/f/notvideo.mkv", 1, tmp_path / "b.jpg")
    assert not r.ok and r.error == "unsupported"
    srv.reset(expire_after_requests=1)
    r = extract.ffmpeg_frame(f"{srv.base}/f/tiny.mkv?sig=SECRET", 3, tmp_path / "c.jpg")
    assert not r.ok and r.error == "http_403_expired"
    assert "SECRET" not in r.proc.stderr


def test_rw_timeout(srv, tmp_path):
    srv.reset(latency_ms=5000)
    t0 = time.perf_counter()
    r = extract.ffmpeg_frame(f"{srv.base}/f/tiny.mkv", 1, tmp_path / "a.jpg",
                             http=extract.HttpOpts(rw_timeout_s=1.0, reconnect=False), timeout_s=30)
    assert not r.ok and r.error == "timeout"
    assert time.perf_counter() - t0 < 4.5


def test_hard_kill_no_orphan(srv, tmp_path):
    srv.reset(latency_ms=10_000)
    r = extract.ffmpeg_frame(f"{srv.base}/f/tiny.mkv", 1, tmp_path / "a.jpg",
                             http=extract.HttpOpts(rw_timeout_s=60), timeout_s=1.0)
    assert r.error == "timeout" and r.proc.killed == "timeout"
    assert r.proc.wall_s < 3
    assert r.proc.orphan is False


def test_cancel_no_orphan(srv, tmp_path):
    srv.reset(latency_ms=10_000)
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()

    def ffmpegs():
        out = set()
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower().startswith("ffmpeg"):
                out.add(p.pid)
        return out

    before = ffmpegs()
    r = extract.ffmpeg_frame(f"{srv.base}/f/tiny.mkv", 1, tmp_path / "a.jpg", cancel=ev,
                             http=extract.HttpOpts(rw_timeout_s=60), timeout_s=30)
    assert r.error == "cancelled" and not r.proc.orphan
    assert r.proc.wall_s < 2
    time.sleep(0.2)
    assert ffmpegs() <= before
