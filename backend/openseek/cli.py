"""Command line: `openseek serve | pair | devices | revoke <id> | dashboard | openapi <file>`.

`pair`, `devices`, `revoke` and `dashboard` talk to the running daemon's loopback admin endpoint
(http://127.0.0.1:<OPENSEEK_ADMIN_PORT>); they never touch the network.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from .config import ConfigError, Settings


def _admin(settings: Settings, method: str, path: str) -> tuple[int, dict | None]:
    from .auth import admin_token
    req = urllib.request.Request(f"http://127.0.0.1:{settings.admin_port}{path}", method=method,
                                 headers={"X-OpenSeek-Admin": admin_token(settings.data_dir, settings.admin_token)})
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
    sub.add_parser("dashboard", help="open the local admin dashboard (the secret travels in the URL fragment, never over the network)")
    rv = sub.add_parser("revoke", help="revoke a paired device")
    rv.add_argument("device_id")
    oa = sub.add_parser("openapi", help="write the API's OpenAPI document")
    oa.add_argument("path")
    cl = sub.add_parser("cloud", help="run OpenSeek Cloud (plain HTTP behind the host's TLS; see docs/CLOUD.md)")
    cl.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    cl.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    a = ap.parse_args(argv)
    if a.cmd == "cloud":
        import uvicorn

        from .cloud.app import create_cloud_app
        uvicorn.run(create_cloud_app(), host=a.host, port=a.port, proxy_headers=False, server_header=False,
                    access_log=False)
        return 0
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    if a.cmd == "serve":
        from .app import serve
        serve(settings)
        return 0
    if a.cmd == "dashboard":
        import webbrowser

        from .auth import admin_token
        url = f"http://127.0.0.1:{settings.admin_port}/#{admin_token(settings.data_dir, settings.admin_token)}"
        print("Opening the dashboard in your browser. The link contains the admin secret: do not share it.")
        if not webbrowser.open(url):
            print(url)
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
