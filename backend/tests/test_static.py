"""Static checks over the service source (SR-10, SR-18)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "openseek"
FILES = sorted(SRC.glob("*.py"))


def test_no_shell_true():
    assert FILES
    for f in FILES:
        text = f.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    assert not (kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)), f
                fn = ast.unparse(node.func)
                assert fn not in ("os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput",
                                  "os.execl", "os.execlp", "os.spawnl", "eval", "exec"), (f, fn)
        assert not re.search(r"shell\s*=\s*True", text), f


def test_no_telemetry_or_unexpected_outbound_libraries():
    banned = ("requests", "urllib3", "sentry_sdk", "opentelemetry", "aiohttp", "socket.create_connection(\"")
    for f in FILES:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for n in names:
                    assert not any(n == b or n.startswith(b + ".") for b in banned[:5]), (f, n)


def test_outbound_connections_only_in_vetted_fetcher():
    """Only netpolicy opens sockets to remote hosts; the worker's reader is loopback-only."""
    for f in FILES:
        text = f.read_text(encoding="utf-8")
        if "create_connection" in text:
            assert f.name == "netpolicy.py", f
        if "HTTPSConnection" in text or "HTTPConnection(" in text:
            assert f.name in ("netpolicy.py", "engine.py"), f
