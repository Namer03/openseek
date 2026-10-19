"""SR-20 redaction rules, and the startup banner the user needs (address + pin) staying readable."""
from __future__ import annotations

import io
import logging

from openseek.logutil import plain, redact, setup_logging


def test_redact_scrubs_urls_tokens_caps_queries():
    tok = "Pe89g3dWsTknOkFg-x--I6O3Ud1TR_qIvKqIeFqr7IQ"
    text = redact(f"GET https://cdn.example.com/dl/abc?sig=SECRET Authorization: Bearer {tok} "
                  f"/v1/c/AbCdEfGhIjKlMnOpQrStUvWxYz012345/x /j/{'a' * 32} code K7QF-M2XD")
    for s in ("cdn.example.com", "SECRET", tok, "AbCdEfGhIjKl", "a" * 32, "K7QF-M2XD", "/dl/abc"):
        assert s not in text, s


def test_banner_unredacted_and_uvicorn_lines_not_duplicated():
    buf = io.StringIO()
    setup_logging("INFO", stream=buf)
    try:
        log = logging.getLogger("openseek.app")
        log.info("%s", "API (HTTPS, LAN): reachable at https://192.168.1.20:8787", extra=plain())
        log.info("%s", "TLS certificate pin: sha256//zChsqgtzwTfeKiLCmZa209smTHMLaNh8FP8Z3d9iC8c=", extra=plain())
        log.info("upstream https://cdn.example.com/x?sig=1")
        uv = logging.getLogger("uvicorn.error")
        for _ in range(2):                       # API + admin servers
            uv.info("Started server process [%d]", 1)
            uv.info("Uvicorn running on %s", "https://0.0.0.0:8787")
            uv.info("Shutting down")
    finally:
        setup_logging("INFO")
    out = buf.getvalue()
    assert "https://192.168.1.20:8787" in out
    assert "sha256//zChsqgtzwTfeKiLCmZa209smTHMLaNh8FP8Z3d9iC8c=" in out
    assert "cdn.example.com" not in out and "sig=1" not in out
    assert "Started server process" not in out and "Uvicorn running on" not in out
    assert out.count("Shutting down") == 1
