"""Command line: `openseek serve | pair | devices | revoke <id> | openapi <file>`.

`pair`, `devices` and `revoke` talk to the running daemon's loopback admin endpoint
(http://127.0.0.1:<OPENSEEK_ADMIN_PORT>); they never touch the network.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from .config import ConfigError, Settings


def _admin(settings: Settings, method: str, path: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(f"http://127.0.0.1:{settings.admin_port}{path}", method=method,
                                 headers={"X-OpenSeek-Admin": "1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never use a proxy
    try:
        with opener.open(req, timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openseek")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the daemon (HTTPS API + loopback admin)")
    sub.add_parser("pair", help="open a 10-minute pairing window and print the code")
    sub.add_parser("devices", help="list paired devices")
    rv = sub.add_parser("revoke", help="revoke a paired device")
    rv.add_argument("device_id")
    oa = sub.add_parser("openapi", help="write the API's OpenAPI document")
    oa.add_argument("path")
    a = ap.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    if a.cmd == "serve":
        from .app import serve
        serve(settings)
        return 0
    if a.cmd == "openapi":
        import tempfile
        from pathlib import Path

        from .app import Core, api_openapi
        with tempfile.TemporaryDirectory() as d:
            core = Core(settings.with_(data_dir=Path(d)))
            try:
                doc = api_openapi(core)
            finally:
                core.stop()
        Path(a.path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    try:
        if a.cmd == "pair":
            st, body = _admin(settings, "POST", "/admin/pair")
            if st != 200 or not body:
                print(f"failed (HTTP {st})", file=sys.stderr)
                return 1
            print(f"Pairing code: {body['code']}  (valid {body['expires_in_s'] // 60} min, single use)")
            print(f"Server pin:   {body['spki_pin']}")
            return 0
        if a.cmd == "devices":
            st, body = _admin(settings, "GET", "/admin/devices")
            if st != 200 or body is None:
                print(f"failed (HTTP {st})", file=sys.stderr)
                return 1
            for d in body["devices"]:
                print(f"{d['device_id']}  {d['name']}")
            return 0
        if a.cmd == "revoke":
            st, _ = _admin(settings, "DELETE", f"/admin/devices/{a.device_id}")
            print("revoked" if st == 204 else f"failed (HTTP {st})")
            return 0 if st == 204 else 1
    except (urllib.error.URLError, OSError):
        print("cannot reach the OpenSeek daemon on the loopback admin port", file=sys.stderr)
        return 1
    return 2
