"""URL / log redaction helpers shared by the test server, the extractor and the bench runner.

Policy (models what production must do): never emit a query string value, and never emit
userinfo. Paths are kept by default for local fixtures; use host_only=True for real
third-party links (TorBox paths can themselves be bearer-like identifiers).
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_URL_RE = re.compile(r"(?i)\b(?:https?|tcp|tls)://[^\s'\"<>]+")


def redact_query(query: str) -> str:
    if not query:
        return ""
    keys = [k for k, _ in parse_qsl(query, keep_blank_values=True)]
    if not keys:
        return "REDACTED"
    return "&".join(f"{k}=REDACTED" for k in keys)


def redact_url(url: str, host_only: bool = False) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    netloc = p.hostname or ""
    if p.port:
        netloc += f":{p.port}"
    if host_only:
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        return urlunsplit((p.scheme, netloc, f"/<path#{digest}>", "", ""))
    q = redact_query(p.query)
    return urlunsplit((p.scheme, netloc, p.path, q, ""))


def redact_path(path_with_query: str) -> str:
    """For server logs: '/f/x.mkv?sig=abc' -> '/f/x.mkv?sig=REDACTED'."""
    if "?" not in path_with_query:
        return path_with_query
    path, q = path_with_query.split("?", 1)
    return f"{path}?{redact_query(q)}"


def redact_text(text: str, host_only: bool = False) -> str:
    """Redact every URL found in free text (e.g. ffmpeg stderr)."""
    return _URL_RE.sub(lambda m: redact_url(m.group(0), host_only=host_only), text)
