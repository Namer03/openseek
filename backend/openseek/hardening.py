"""Start-up checks for deployments the server cannot protect by itself. They only ever warn (never fail, never change anything):

* Windows: a data directory whose permissions let other local users read it (it holds the TLS private key, the capability key,
  the admin secret and the invitation database). The default `%LOCALAPPDATA%\\OpenSeek\\data` is private; a folder at the root of another
  drive usually is not.
* Containers: behind Docker Desktop's NAT or any proxy every client appears with a private address, so the "private clients only"
  check cannot tell a LAN device from the internet.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable

# principals that mean "other local users"
BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users", "NT AUTHORITY\\INTERACTIVE", "BUILTIN\\Guests")
_GRANTS = {"F", "M", "W", "R", "RX", "GR", "GA", "GW"}          # read/write/full (not execute-only or delete-child)


def broad_acl_principals(icacls_output: str) -> list[str]:
    """Principals in `icacls <dir>` output that hold an ALLOW entry giving read or write access to other local users."""
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
            if perms & _GRANTS and p not in found:
                found.append(p)
    return found


def check_data_dir(path: Path, warn: Callable[[str], None]) -> list[str]:
    """Windows only: warn when the data directory is readable by other local users. Returns the offending principals."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
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


CONTAINER_WARNING = ("running inside a container: clients reach the owner API through the container network, so the 'private clients only' "
                     "check cannot tell your LAN from the internet. Publish the port only on 127.0.0.1 or on your LAN address "
                     "(never 8787:8787 / 0.0.0.0) and never forward it from your router")
