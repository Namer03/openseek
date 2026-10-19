"""The real `openseek serve` process with the public listener: restart/recovery, bind addresses, and that no secret ever
reaches the logs or the command line."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from helpers import free_port
from openseek import pairing

BACKEND = Path(__file__).resolve().parent.parent
ADMIN = {"X-OpenSeek-Admin": "test-admin-token"}


def start(data: Path, api: int, admin: int, public: int | None, tag: str):
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("OPENSEEK_")]:
        del env[k]
    env.update(OPENSEEK_DATA_DIR=str(data), OPENSEEK_HOST="127.0.0.1", OPENSEEK_PORT=str(api), OPENSEEK_ADMIN_PORT=str(admin), OPENSEEK_ADMIN_TOKEN="test-admin-token")
    if public:
        env.update(OPENSEEK_PUBLIC_PORT=str(public), OPENSEEK_PUBLIC_BIND="127.0.0.1", OPENSEEK_PUBLIC_HOST=f"127.0.0.1:{public}")
    out, err = data / f"{tag}.out.log", data / f"{tag}.err.log"
    p = subprocess.Popen([sys.executable, "-m", "openseek", "serve"], cwd=BACKEND, env=env,
                         stdout=open(out, "wb"), stderr=open(err, "wb"))
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{admin}/admin/status", headers=ADMIN, timeout=1).status_code == 200:
                return p, out, err
        except httpx.HTTPError:
            time.sleep(0.2)
    p.kill()
    raise AssertionError("server did not start: " + err.read_text(errors="replace")[-600:])


def stop(p) -> None:
    p.terminate()
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()


def listeners(*ports: int) -> dict[int, str]:
    """{port: local address} of the LISTENING sockets among `ports` (the venv launcher may own them via a child process)."""
    ps = ("Get-NetTCPConnection -State Listen | Where-Object { @(" + ",".join(map(str, ports)) + ") -contains $_.LocalPort } | "
          "ForEach-Object { \"$($_.LocalPort) $($_.LocalAddress) $($_.OwningProcess)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.splitlines()
    return {int(a.split()[0]): a.split()[1] for a in out if a.strip()}


def owner_cmdline(port: int) -> str:
    ps = (f"$p=(Get-NetTCPConnection -State Listen -LocalPort {port}).OwningProcess | Select-Object -First 1;"
          "(Get-CimInstance Win32_Process -Filter \"ProcessId=$p\").CommandLine")
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout


def test_real_process_restart_bind_addresses_and_no_secrets_in_logs_or_command_line(tmp_path):
    api, admin, public = free_port(), free_port(), free_port()
    data = tmp_path / "data"
    data.mkdir()
    secrets: list[str] = []
    p, out1, err1 = start(data, api, admin, public, "run1")
    try:
        ls = listeners(api, admin, public)
        assert ls[admin] in ("127.0.0.1", "::1"), ls                      # the dashboard is loopback only
        assert ls[public] == "127.0.0.1" and ls[api] == "127.0.0.1", ls   # (a real deployment binds 0.0.0.0 for both API and public)
        cl = owner_cmdline(api)
        assert "openseek" in cl and "serve" in cl and "token" not in cl.lower() and "duckdns" not in cl.lower()
        with httpx.Client(base_url=f"http://127.0.0.1:{admin}", headers=ADMIN, timeout=30) as adm:
            adm.put("/admin/sharing", json={"enabled": True})
            adm.put("/admin/library", json={"metadata_index": True})
            inv = adm.post("/admin/invites", json={"public": True, "label": "her TV"}).json()
            assert inv["address"] == f"127.0.0.1:{public}"
            secrets += [inv["secret"], inv["code"], inv["code"].replace("-", ""), inv["join"]]
        base = f"https://127.0.0.1:{public}"
        with httpx.Client(base_url=base, verify=False, timeout=30) as c:
            s = c.post("/v1/pair/start", json={"invite": inv["invite_id"]}).json()
            k = pairing.derive_key(inv["secret"], s["pair_id"], s["salt"], s["spki_pin"], s["iterations"])
            r = c.post("/v1/pair/claim", json={"pair_id": s["pair_id"], "device_name": "her-tv",
                                               "client_proof": pairing.client_proof(k, s["pair_id"], "her-tv")}).json()
            assert r["role"] == "guest" and r["server_proof"] == pairing.server_proof(k, s["pair_id"], r["token"])
            token = r["token"]
            secrets.append(token)
        with httpx.Client(base_url=base, verify=False, timeout=30, headers={"Authorization": "Bearer " + token}) as g:
            assert g.get("/v1/pair/status").status_code == 200
            assert g.post("/v1/lookup", json={"imdb_id": "tt0000009", "duration_ms": 600000}).status_code == 404
        # ---- restart: same data directory, same guest, nothing re-paired
        stop(p)
        p, out2, err2 = start(data, api, admin, public, "run2")
        with httpx.Client(base_url=base, verify=False, timeout=30, headers={"Authorization": "Bearer " + token}) as g:
            assert g.get("/v1/pair/status").status_code == 200
        # ---- rollback: the same data WITHOUT the public setting -> no public listener at all, API and admin as before
        stop(p)
        p, out3, err3 = start(data, api, admin, None, "run3")
        ls = listeners(api, admin, public)
        assert public not in ls and api in ls and admin in ls, ls
        with pytest.raises(httpx.TransportError):
            httpx.get(base + "/v1/health", verify=False, timeout=3)
    finally:
        stop(p)
    # ---- no secret (token, invite secret, typed code, join string) in any log of any run
    blob = "".join(f.read_text(errors="replace") for f in data.glob("run*.log"))
    assert blob.strip(), "the logs are empty - the scan would prove nothing"
    for sct in secrets:
        assert sct not in blob, "a secret reached a log"
    # access lines carry the path only; a capability URL's signature is not in the logs either
    assert "Bearer" not in blob and "client_proof" not in blob
