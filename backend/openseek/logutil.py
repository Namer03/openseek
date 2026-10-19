"""Logging with redaction (SR-20).

Rules: log output never contains query strings, userinfo, bearer tokens, capability
segments, pairing codes or forwarded headers; hosts are reduced to a short salted hash.
Our own code never passes URLs to the logger in the first place (it logs `host_tag()`
values); the formatter-level scrubber below is defence in depth for exception text and
third-party log lines (uvicorn access log).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import sys

_SALT = os.urandom(16)  # per-process: host tags are not linkable across restarts

_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15})://([^\s'\"<>/?#]*)([^\s'\"<>]*)")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_CAP_RE = re.compile(r"/v1/c/[^/\s?\"']+")
_PROXY_RE = re.compile(r"/j/[0-9a-fA-F]{8,}")
_QUERY_RE = re.compile(r"\?[^\s\"']*")
_TOKENISH_RE = re.compile(r"\b[A-Za-z0-9_-]{43}\b")          # 32-byte base64url tokens
_CODE_RE = re.compile(r"\b[2-9A-HJKMNP-Z]{4}-?[2-9A-HJKMNP-Z]{4}\b")  # pairing code shape
_HDR_RE = re.compile(r"(?i)\b(user-agent|referer|origin|cookie|authorization)\s*[:=]\s*[^\r\n,}]+")


def host_tag(host: str | None) -> str:
    """Short salted hash of a hostname, safe to log."""
    if not host:
        return "h:-"
    d = hmac.new(_SALT, host.lower().encode("utf-8", "replace"), hashlib.sha256).hexdigest()
    return "h:" + d[:8]


def _url_sub(m: re.Match) -> str:
    scheme, netloc = m.group(1), m.group(2)
    hostport = netloc.rsplit("@", 1)[-1]
    host = hostport
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    elif ":" in host:
        host = host.split(":")[0]
    return f"{scheme.lower()}://{host_tag(host)}/[redacted]"


def redact(text: str) -> str:
    if not text:
        return text
    text = _URL_RE.sub(_url_sub, text)
    text = _BEARER_RE.sub(r"\1 [redacted]", text)
    text = _CAP_RE.sub("/v1/c/[cap]", text)
    text = _PROXY_RE.sub("/j/[cap]", text)
    text = _QUERY_RE.sub("?[redacted]", text)
    text = _HDR_RE.sub(lambda m: m.group(1) + ": [redacted]", text)
    text = _TOKENISH_RE.sub("[redacted-token]", text)
    text = _CODE_RE.sub("[redacted-code]", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Scrubs every record, except the daemon's own startup banner (its bind address, LAN
    addresses and certificate pin are what the user needs to configure the TV). Only code in
    this package sets `openseek_plain`, and only for values it generated itself."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if getattr(record, "openseek_plain", False):
            return text
        return redact(text)

    def formatException(self, ei) -> str:  # noqa: N802 - logging API
        return redact(super().formatException(ei))


class AccessLogFilter(logging.Filter):
    """uvicorn.access passes (client, method, path, http_version, status) as args.
    Strip the query string and capability segment before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2].split("?", 1)[0]
            path = _CAP_RE.sub("/v1/c/[cap]", path)
            record.args = (args[0], args[1], path) + tuple(args[3:])
        return True


class UvicornNoiseFilter(logging.Filter):
    """The daemon runs two uvicorn servers (API + admin); their per-server lifecycle lines would
    appear twice and "Uvicorn running on <url>" duplicates our banner. Drop those."""

    DROP = ("Started server process", "Waiting for application", "Application startup complete",
            "Uvicorn running on", "Finished server process", "Application shutdown complete",
            "Waiting for application shutdown")

    def __init__(self):
        super().__init__()
        self._last = ("", 0.0)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg.startswith(self.DROP):
            return False
        now = record.created
        if msg == self._last[0] and now - self._last[1] < 2.0:
            return False                      # e.g. "Shutting down" from both servers
        self._last = (msg, now)
        return True


def plain(**kw) -> dict:
    """`extra=` for the startup banner (not redacted)."""
    return {"openseek_plain": True, **kw}


_configured_handler: logging.Handler | None = None


def setup_logging(level: str = "INFO", stream=None) -> logging.Handler:
    """Install one redacting handler on the root logger (idempotent; replaces the previous one)."""
    global _configured_handler
    root = logging.getLogger()
    if _configured_handler is not None:
        root.removeHandler(_configured_handler)
    h = logging.StreamHandler(stream or sys.stderr)
    h.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    acc = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogFilter) for f in acc.filters):
        acc.addFilter(AccessLogFilter())
    err = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, UvicornNoiseFilter) for f in err.filters):
        err.addFilter(UvicornNoiseFilter())
    _configured_handler = h
    return h
