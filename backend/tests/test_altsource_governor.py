from __future__ import annotations

from openseek.altsource import Candidate, plan_attempts, reject_reason
from openseek.db import DB
from openseek.governor import Governor, Limits

PLAY = Candidate("p", "tt1856101", 2160, 9_760_000, 60_000_000_000, "hevc", "mkv",
                 "Blade.Runner.2049.2160p.REMUX", cached=True)


def cand(cid, **kw):
    d = dict(title_key="tt1856101", height=720, duration_ms=9_760_000, size=4_000_000_000, codec="h264",
             container="mkv", name="Blade.Runner.2049.720p.BluRay.x264", cached=True)
    d.update(kw)
    return Candidate(cid, **d)


def test_eligibility_rules():
    assert reject_reason(PLAY, cand("a")) is None
    assert reject_reason(PLAY, cand("a", cached=False)) == "not_cached"            # never start a download
    assert reject_reason(PLAY, cand("a", title_key="tt0000001")) == "other_title"
    assert reject_reason(PLAY, cand("a", height=2160)) == "resolution"
    assert reject_reason(PLAY, cand("a", height=360)) == "resolution"
    assert reject_reason(PLAY, cand("a", duration_ms=12_000_000)) == "duration"
    assert reject_reason(PLAY, cand("a", name="Blade.Runner.2049.EXTENDED.720p")) == "edition"
    assert reject_reason(PLAY, cand("a", codec="vc1")) == "codec"
    assert reject_reason(PLAY, cand("a", container="avi")) == "container"


def test_ranking_prefers_held_link_then_720_then_low_bitrate_and_bounds_attempts():
    a = cand("1080", height=1080, size=9_000_000_000)
    b = cand("720big", size=8_000_000_000)
    c = cand("720small", size=3_000_000_000)
    d = cand("720held", size=8_000_000_000, resolved=True)
    plan = plan_attempts(PLAY, [a, b, c, d])
    assert [x.cid for x in plan] == ["720held", "720small"]                       # <= 2 resolutions per request


def make_gov(**kw):
    t = [0.0]
    return Governor(Limits(**kw), clock=lambda: t[0]), t


def test_governor_starts_off_grows_when_healthy_and_yields_on_rebuffer():
    g, t = make_gov(grow_after=3)
    assert g.allowed("h") == 0                                                     # no telemetry: stay off the CDN
    g.playback(20_000, False, "h")
    assert g.allowed("h") == 1
    for _ in range(9):
        g.completed(0.2, True)
    assert 1 < g.conc <= g.l.max_conc
    g.playback(500, True, "h")                                                     # rebuffering
    assert g.allowed("h") == 0 and g.conc == 1
    g.playback(20_000, False, "h")
    for _ in range(30):
        g.completed(0.2, True)
    assert g.conc == 1                                                             # held down by hysteresis
    t[0] += g.l.resume_after_s + 1
    g.playback(20_000, False, "h")
    for _ in range(30):
        g.completed(0.2, True)
    assert g.conc > 1


def test_429_pauses_host_and_ceilings_hold():
    g, t = make_gov(grow_after=1, max_conc=3, host_budget=4)
    g.playback(20_000, False, "h", playback_conns=2)
    for _ in range(20):
        g.completed(0.1, True)
    assert g.conc <= 3 and g.allowed("h") <= 2                                     # host budget minus playback conns
    g.retry_after("h", 30)
    assert g.allowed("h") == 0
    t[0] += 31
    g.playback(20_000, False, "h")
    assert g.allowed("h") >= 1
    assert g.try_acquire("h") and g.inflight("h") == 1
    g.release("h")
    assert g.inflight("h") == 0


def test_stale_telemetry_pauses_then_is_lost():
    g, t = make_gov()
    assert g.status("h") == "none"
    g.playback(20_000, False, "h")
    g.conc = 3
    t[0] += g.l.telemetry_stale_s + 1
    assert g.status("h") == "stale" and g.allowed("h") == 0 and g.conc == 1     # wait, never push
    t[0] += g.l.telemetry_lost_s                                                  # gone for good
    assert g.status("h") == "lost"


def test_retry_after_and_rebuffer_have_their_own_status_and_health():
    g, t = make_gov()
    g.playback(20_000, False, "h")
    assert g.status("h") == "ok" and g.healthy("h")
    g.retry_after("h", 20)
    assert g.status("h") == "pause" and not g.healthy("h") and g.healthy("other")
    t[0] += 21
    g.playback(100, True, "h")
    assert g.status("h") == "rebuffer" and not g.healthy()


def test_provenance_roundtrip(tmp_path):
    db = DB(tmp_path / "x.db")
    db.put_thumb_source("a" * 64, "b" * 64, 1.0, 7000.0, 2100.0, "offset")
    assert db.get_thumb_source("a" * 64)[:2] == ("b" * 64, 1.0)
    assert db.get_thumb_source("c" * 64) is None


def test_startup_buffering_is_not_a_rebuffer_but_a_mid_playback_stall_is():
    g, t = make_gov()
    g.playback(None, True, "h")                         # the player is still loading: not a stall
    for _ in range(3):
        g.playback(None, True, "h")
    assert g.rebuffer_events == 0 and g.startup_buffering_events == 1
    for _ in range(12):                                  # steady playback
        g.playback(None, False, "h")
    g.playback(None, True, "h")                          # now it really stalls
    assert g.rebuffer_events == 1 and g.status("h") == "rebuffer"
    g.playback(None, False, "h")
    g.playback(None, True, "h")                          # a seek right after: still start-up buffering
    assert g.rebuffer_events == 1 and g.startup_buffering_events == 2


def test_hybrid_and_dubbed_are_not_editions_but_3d_and_open_matte_are():
    play = Candidate("p", "tt1856101", 2160, 0, 90_000_000_000, "", "mkv",
                     "Blade.Runner.2049.2017.2160p.UHD.BDRemux.TrueHD.Atmos.HDR.DoVi.Hybrid.P8.by.DVT.mkv", cached=True)
    ok = lambda name, **kw: reject_reason(play, cand("c", name=name, height=1080, duration_ms=0, **kw))   # noqa: E731
    assert ok("Blade.Runner.2049.2017.1080p.BluRay.REMUX.AVC.mkv") is None            # same cut, different release
    assert ok("Blade.Runner.2049.2017.1080p.BluRay.DUBBED.mkv") is None
    for bad in ("Blade.Runner.2049.2017.BD3D.1080p.BluRay.REMUX.AVC", "Blade.Runner.2049.2017.1080p.3D.BluRay.Half-SBS.x264",
                "Blade.Runner.2049.2017.Open.Matte.MULTI.1080p.BluRay", "Blade.Runner.2049.EXTENDED.1080p"):
        assert ok(bad) == "edition", bad
    hy = Candidate("p2", "tt1856101", 2160, 0, 90_000_000_000, "", "mkv", "Blade.Runner.2049.2160p.REMUX.mkv", cached=True)
    assert reject_reason(hy, cand("c", name="Blade.Runner.2049.1080p.Hybrid.mkv", height=1080, duration_ms=0)) is None


def test_unknown_runtime_ranks_smaller_files_first():
    big = cand("big", height=1080, duration_ms=0, size=49_000_000_000)
    small = cand("small", height=1080, duration_ms=0, size=30_000_000_000)
    p720 = cand("p720", height=720, duration_ms=0, size=9_000_000_000)
    assert [c.cid for c in plan_attempts(PLAY, [big, small, p720])] == ["p720", "small"]


def test_buffering_right_after_a_seek_is_not_counted_as_a_stall():
    g, t = make_gov()
    for _ in range(12):
        g.playback(None, False, "h")                       # steady playback
    g.playback(None, True, "h", seeking=True)               # the person scrubbed; the player re-buffers
    assert g.rebuffer_events == 0 and g.seek_buffering_events == 1
    assert g.status("h") == "rebuffer"                      # extraction still waits for playback to recover
    g.playback(None, False, "h")
    for _ in range(12):
        g.playback(None, False, "h")
    g.playback(None, True, "h")                             # a stall with no seek: this one counts
    assert g.rebuffer_events == 1
