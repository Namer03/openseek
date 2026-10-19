"""Retry-After: numeric and HTTP-date forms, clamping, defaults, and the Upstream/governor path (no network)."""
from __future__ import annotations

import datetime
import email.utils
import time

import pytest

from helpers import make_policy
from origin import Origin
from openseek.fetch import (Budget, Upstream, UpstreamError, parse_retry_after, rate_limit_error,
                            RETRY_AFTER_DEFAULT_429_S, RETRY_AFTER_MAX_S)

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()


def http_date(offset_s: float, fmt: str = "imf") -> str:
    dt = datetime.datetime.fromtimestamp(NOW + offset_s, datetime.timezone.utc)
    if fmt == "imf":
        return email.utils.format_datetime(dt, usegmt=True)                 # Sun, 18 Oct 2026 12:01:30 GMT
    if fmt == "rfc850":
        return dt.strftime("%A, %d-%b-%y %H:%M:%S GMT")                    # Sunday, 18-Oct-26 12:01:30 GMT
    return dt.strftime("%a %b %d %H:%M:%S %Y")                              # asctime: Sun Oct 18 12:01:30 2026


@pytest.mark.parametrize("value,expected", [
    ("120", 120.0), ("0", 0.0), ("  45  ", 45.0), ("1", 1.0),
    ("999999", RETRY_AFTER_MAX_S),                                          # clamped
])
def test_numeric_delta_seconds(value, expected):
    assert parse_retry_after(value, now=NOW) == expected


@pytest.mark.parametrize("fmt", ["imf", "rfc850", "asctime"])
def test_http_date_forms_are_relative_to_now(fmt):
    assert parse_retry_after(http_date(90, fmt), now=NOW) == pytest.approx(90.0, abs=1.0)


def test_http_date_in_the_past_is_zero_and_far_future_is_clamped():
    assert parse_retry_after(http_date(-300), now=NOW) == 0.0
    assert parse_retry_after(http_date(10 * 86400), now=NOW) == RETRY_AFTER_MAX_S


@pytest.mark.parametrize("value", [None, "", "   ", "soon", "-5", "1.5", "12abc", "Not, a date", "Sun, 99 Foo 2026 99:99:99 GMT"])
def test_unparseable_values_are_none(value):
    assert parse_retry_after(value, now=NOW) is None


def test_429_always_carries_a_backoff_503_only_with_a_usable_header():
    assert rate_limit_error(429, None).retry_after == RETRY_AFTER_DEFAULT_429_S      # missing -> default
    assert rate_limit_error(429, "junk").retry_after == RETRY_AFTER_DEFAULT_429_S    # unreadable -> default
    assert rate_limit_error(429, "7").retry_after == 7.0
    assert rate_limit_error(429, "0").retry_after == 0.0                             # an explicit 0 is honoured
    assert rate_limit_error(503, None).retry_after is None
    assert rate_limit_error(503, "15").retry_after == 15.0
    assert rate_limit_error(404, "15").retry_after is None
    assert isinstance(rate_limit_error(429, "9"), UpstreamError)


HOST = "media.example.com"


def _upstream(origin, name: str, **kw):
    pol = make_policy([origin], {HOST: ["127.0.0.1"]})
    return Upstream(pol, f"http://{HOST}:{origin.port}/f/{name}", {}, Budget(10 * 1024 ** 3), 5.0)


def _real_date(offset_s: float) -> str:
    dt = datetime.datetime.fromtimestamp(time.time() + offset_s, datetime.timezone.utc)
    return email.utils.format_datetime(dt, usegmt=True)


@pytest.mark.parametrize("header", ["45", "0", "_future_date", "_past_date"])
def test_preflight_429_surfaces_retry_after_and_is_counted(media, header):
    """Every form, including 0 and a date already in the past, is a rate limit: counted, reported, never retried."""
    header = {"_future_date": _real_date(120), "_past_date": _real_date(-120)}.get(header, header)
    o = Origin(media).start()
    try:
        o.reset(raw={"/f/main.mkv": (429, "text/plain", b"slow down", {"Retry-After": header})})
        up = _upstream(o, "main.mkv")
        with pytest.raises(UpstreamError) as ei:
            up.preflight()
        assert ei.value.retry_after is not None
        if header == "45":
            assert ei.value.retry_after == 45.0
        snap = up.stats.snapshot()
        assert snap["rate_limited"] == 1 and snap["by_status"].get("429") == 1
        assert snap["resolutions"] == 1                       # one request of the original URL, no retry
        up.close()
    finally:
        o.stop()


@pytest.mark.parametrize("header,expected", [("33", 33.0), (None, RETRY_AFTER_DEFAULT_429_S)])
def test_mid_stream_429_on_a_range_read_is_reported_not_retried(media, header, expected):
    o = Origin(media).start()
    try:
        up = _upstream(o, "main.mkv")
        pre = up.preflight()
        assert pre.size > 0
        before = up.stats.snapshot()["requests"]
        hdrs = {"Retry-After": header} if header else {}
        o.reset(raw={"/f/main.mkv": (429, "text/plain", b"slow", hdrs)})
        with pytest.raises(UpstreamError) as ei:
            up.read_bytes(1_000_000, 1_000_999)
        assert ei.value.retry_after == expected
        snap = up.stats.snapshot()
        assert snap["requests"] == before + 1                  # exactly one request: never retried inside
        assert snap["rate_limited"] == 1 and snap["retry_after_max_s"] == expected
        up.close()
    finally:
        o.stop()


def test_stats_count_resolutions_hops_latency_and_concurrency(media):
    o = Origin(media).start()
    try:
        o.reset(redirects={"/r/main.mkv": f"/f/main.mkv"})
        pol = make_policy([o], {HOST: ["127.0.0.1"]})
        up = Upstream(pol, f"http://{HOST}:{o.port}/r/main.mkv", {}, Budget(10 * 1024 ** 3), 5.0)
        up.preflight()
        snap = up.stats.snapshot()
        assert snap["resolutions"] == 1 and snap["redirect_hops"] == 1       # ONE resolution of the original link
        for i in range(4):
            up.read_bytes(i * 4096, i * 4096 + 4095)
        snap = up.stats.snapshot()
        assert snap["resolutions"] == 1                                       # range reads never re-resolve
        assert snap["latency_ms_p50"] is not None and snap["max_inflight"] >= 1 and snap["inflight"] == 0
        assert snap["by_status"].get("206", 0) >= 4
        up.close()
    finally:
        o.stop()
