"""Start-up checks for deployments the server cannot protect by itself. They only ever warn (never fail, never change anything):

* Windows: a data directory whose permissions let other local users read it (it holds the TLS private key, the capability key,
  the admin secret and the invitation database). The default `%LOCALAPPDATA%\\OpenSeek\\data` is private; a folder at the root of another
  drive usually is not.
* Containers: behind Docker Desktop's NAT or any proxy every client appears with a private address, so the "private clients only"
  check cannot tell a LAN device from the internet.
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

# principals that mean "other local users"
BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users", "NT AUTHORITY\\INTERACTIVE", "BUILTIN\\Guests")
_GRANTS = {"F", "M", "W", "R", "RX", "GR", "GA", "GW"}          # read/write/full (not execute-only or delete-child)


_WRITES = {"F", "M", "W", "GA", "GW"}


def broad_acl_principals(icacls_output: str, writes_only: bool = False) -> list[str]:
    """Principals in `icacls <dir>` output that hold an ALLOW entry giving read (or, with writes_only, write) access to other local users."""
    grants = _WRITES if writes_only else _GRANTS
    found: list[str] = []
    for line in icacls_output.splitlines():
        for p in BROAD_PRINCIPALS:
            m = re.search(r"(?<![\w\\])" + re.escape(p) + r":(\(.*)$", line.strip(), re.IGNORECASE)
            if not m:
                continue
            rest = m.group(1)
            if "(DENY)" in rest.upper():
                continue
            perms = {x.strip().upper() for grp in re.findall(r"\(([^)]*)\)", rest) for x in grp.split(",")}
            if perms & grants and p not in found:
                found.append(p)
    return found


def _icacls(path: Path) -> str | None:
    if os.name != "nt":
        return None
    try:
        return subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def data_dir_writable_by_others(path: Path) -> list[str]:
    """Windows only: other local users who can WRITE to the data directory (they could replace the database, key or admin secret).
    The server refuses to start in that case unless OPENSEEK_ALLOW_SHARED_DATA_DIR=1."""
    out = _icacls(path)
    return broad_acl_principals(out, writes_only=True) if out else []


def check_data_dir(path: Path, warn: Callable[[str], None]) -> list[str]:
    """Windows only: warn when the data directory is readable by other local users. Returns the offending principals."""
    out = _icacls(path)
    if not out:
        return []
    bad = broad_acl_principals(out)
    if bad:
        warn("the data directory %s is accessible to: %s. It holds the TLS private key, secret.key, admin.token and the invitation database; "
             "move it into your own profile (the default %%LOCALAPPDATA%%\\OpenSeek\\data) or remove those entries (icacls <dir> /inheritance:r "
             "/grant:r \"%%USERNAME%%:(OI)(CI)F\")" % (path, ", ".join(bad)))
    return bad


def running_in_container() -> bool:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        txt = Path("/proc/1/cgroup").read_text(errors="replace")
    except OSError:
        return False
    return any(k in txt for k in ("docker", "kubepods", "containerd", "libpod"))


_LAN_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))   # not documentation, CGNAT or link-local ranges


def published_on_error(value: str | None) -> str | None:
    """Inside a container the owner API's TCP peer address is the Docker bridge or a proxy, so it cannot tell your LAN from the internet.
    The operator must therefore DECLARE the host address the port is published on (the compose file passes the same variable to the port
    mapping and to this check), and it must be loopback or a private LAN address. Returns why it is refused, or None if acceptable.
    `host-network` is accepted for Linux host networking, where the real client addresses are visible and the private-address check works."""
    hint = ("Use the supplied docker-compose.yml (docker compose up -d for this computer only, tools/docker-lan.sh or tools/docker-lan.ps1 for LAN devices), "
            "or pass -e OPENSEEK_PUBLISHED_ON=<the address you publish on> together with -p <that address>:8787:8787")
    if not value or not value.strip():
        return "OPENSEEK_PUBLISHED_ON is not set. " + hint
    if value.strip() == "host-network":
        return None
    for part in (p.strip() for p in value.split(",")):
        try:
            ip = ipaddress.ip_address(part)
        except ValueError:
            return "OPENSEEK_PUBLISHED_ON=%r is not an IP address. %s" % (part, hint)
        if ip.is_unspecified:
            return "OPENSEEK_PUBLISHED_ON=%s means all interfaces; publishing the owner API that way is refused. %s" % (part, hint)
        if ip.is_loopback:
            continue
        if not any(ip in net for net in _LAN_NETS):
            return "OPENSEEK_PUBLISHED_ON=%s is not a loopback or private LAN address; the owner API must never be published on it. %s" % (part, hint)
    return None


CONTAINER_NOTE = ("running inside a container: the owner API is published on %s. Clients are authenticated by their device token; the "
                  "'private clients only' address check cannot see real client addresses here and is not relied on")


def refuse_shared_data_dir(data_dir) -> None:
    """Fail closed (Windows): do not start, and do not create any key material, in a folder other local users can write to.
    A data directory that does not exist yet is judged by its nearest existing parent, because it will inherit that folder's permissions."""
    if os.environ.get("OPENSEEK_ALLOW_SHARED_DATA_DIR") == "1":
        return
    p = Path(data_dir).absolute()
    while not p.exists() and p.parent != p:
        p = p.parent
    writers = data_dir_writable_by_others(p)
    if writers:
        raise SystemExit(
            "refusing to start: other local users (%s) can WRITE to %s, which will hold the database, the TLS key and the admin secret. "
            "Use a folder inside your own profile (the default is %%LOCALAPPDATA%%\\OpenSeek\\data) or fix the permissions. "
            "Set OPENSEEK_ALLOW_SHARED_DATA_DIR=1 only if you accept that risk." % (", ".join(writers), p))
