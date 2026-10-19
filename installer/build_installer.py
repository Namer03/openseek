"""Build OpenSeekSetup-v<version>-Windows-x64.exe (one installer: server as a Windows service + desktop app).

    python installer/build_installer.py [--version 0.2.0] [--build-server] [--build-desktop] [--sources-archives DIR]

Needs: 64-bit CPython 3.14 on Windows and Inno Setup 6 (ISCC.exe; found on PATH or in the default folders, or pass --iscc).
Steps: (1) build the frozen server (backend/build_windows_server.py) and the desktop app (desktop/build_windows.py), unless
--no-build is given and their outputs already exist; (2) fetch WinSW (pinned SHA-256); (3) stage everything with the licences, the source offer
and the notices under installer/.build/stage; (4) compile the installer; (5) write its .sha256. With --sources-archives it also zips the
corresponding third-party source archives (the FFmpeg/x264/x265/... sources the source offer promises) next to the installer.
Nothing outside installer/.build, backend/.build-windows-server and desktop/.build is touched.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / ".build"
STAGE = BUILD / "stage"
OUT = BUILD / "out"
WINSW_URL = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe"
WINSW_SHA256 = "b5066b7bbdfba1293e5d15cda3caaea88fbeab35bd5b38c41c913d492aadfc4f"
WINSW_LICENSE_URL = "https://raw.githubusercontent.com/winsw/winsw/v2.12.0/LICENSE.txt"


def run(cmd, **kw):
    print(">", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def find_iscc(given: str | None) -> Path:
    cands = [given, os.environ.get("OPENSEEK_ISCC"), shutil.which("ISCC"),
             r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe",
             str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe")]
    for c in cands:
        if c and Path(c).exists():
            return Path(c)
    sys.exit("Inno Setup 6 (ISCC.exe) not found: install it from https://jrsoftware.org/isinfo.php or pass --iscc")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="0.2.0")
    ap.add_argument("--iscc")
    ap.add_argument("--build-server", action="store_true", help="(re)build the frozen server first")
    ap.add_argument("--build-desktop", action="store_true", help="(re)build the desktop app first")
    ap.add_argument("--server-venv", help="venv whose packages are bundled in the server (for licence collection)")
    ap.add_argument("--desktop-venv", help="venv used for the desktop build")
    ap.add_argument("--sources-archives", help="folder with the third-party source archives to zip as a release asset")
    ap.add_argument("--release-tag", default=None)
    a = ap.parse_args()
    v = a.version
    tag = a.release_tag or f"v{v}"
    iscc = find_iscc(a.iscc)

    server_dist = ROOT / "backend" / ".build-windows-server" / "server" / "dist" / "OpenSeekServer"
    desktop_dist = ROOT / "desktop" / ".build" / "dist" / "OpenSeek"
    if a.build_server or not server_dist.exists():
        run([sys.executable, ROOT / "backend" / "build_windows_server.py"])
    if a.build_desktop or not desktop_dist.exists():
        cmd = [sys.executable, ROOT / "desktop" / "build_windows.py", "--version", v]
        if a.desktop_venv:
            cmd += ["--venv", a.desktop_venv]
        run(cmd)
    for d in (server_dist, desktop_dist):
        if not (d / ("OpenSeekServer.exe" if "Server" in d.name else "OpenSeek.exe")).exists():
            sys.exit(f"missing build output: {d}")

    BUILD.mkdir(exist_ok=True)
    winsw = BUILD / "WinSW.NET461.exe"
    if not winsw.exists():
        print("downloading WinSW 2.12.0 ...")
        urllib.request.urlretrieve(WINSW_URL, winsw)
    if sha256(winsw) != WINSW_SHA256:
        sys.exit("WinSW checksum mismatch: refusing to use it")
    winsw_lic = BUILD / "WinSW-LICENSE.txt"
    if not winsw_lic.exists():
        urllib.request.urlretrieve(WINSW_LICENSE_URL, winsw_lic)

    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    shutil.copytree(server_dist, STAGE / "server")
    shutil.copytree(desktop_dist, STAGE / "desktop")
    for d in list(STAGE.rglob("__pycache__")):            # compiled files in collected metadata embed the build machine's paths
        shutil.rmtree(d, ignore_errors=True)
    (STAGE / "service").mkdir()
    shutil.copyfile(winsw, STAGE / "service" / "OpenSeekService.exe")

    # licences: third-party (FFmpeg etc.), WinSW, desktop (Qt...) and the server's Python packages
    lic = STAGE / "licenses"
    shutil.copytree(HERE / "third-party" / "licenses", lic / "third-party")
    shutil.copyfile(HERE / "third-party" / "SOURCES-MANIFEST.md", lic / "third-party" / "SOURCES-MANIFEST.md")
    (lic / "WinSW").mkdir()
    shutil.copyfile(winsw_lic, lic / "WinSW" / "LICENSE.txt")
    desk_py = Path(a.desktop_venv) / "Scripts" / "python.exe" if a.desktop_venv else Path(sys.executable)
    run([desk_py, ROOT / "desktop" / "collect_licenses.py", lic / "desktop", BUILD / "desktop-notices.txt"])
    srv_py = Path(a.server_venv) / "Scripts" / "python.exe" if a.server_venv else Path(sys.executable)
    pkgs = [ln.split("==")[0] for ln in (ROOT / "backend" / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    run([srv_py, ROOT / "desktop" / "collect_licenses.py", lic / "server-python-packages", BUILD / "server-notices.txt"],
        env={**os.environ, "OPENSEEK_LICENSE_PACKAGES": ",".join(pkgs)})

    today = datetime.date.today().isoformat()
    for name in ("SOURCE-OFFER.txt", "FFMPEG-NOTICE.txt"):
        t = (HERE / "third-party" / name).read_text(encoding="utf-8").replace("<RELEASE-TAG>", tag).replace("<RELEASE-DATE>", today)
        (STAGE / name).write_text(t, encoding="utf-8", newline="\r\n")
    shutil.copyfile(ROOT / "LICENSE", STAGE / "LICENSE")
    (STAGE / "installer-license.txt").write_text(
        (ROOT / "LICENSE").read_text(encoding="utf-8") + "\n\n" + "=" * 78 + "\nTHIRD-PARTY SOFTWARE IN THIS INSTALLER\n" + "=" * 78 + "\n\n"
        + (STAGE / "FFMPEG-NOTICE.txt").read_text(encoding="utf-8"), encoding="utf-8", newline="\r\n")
    (STAGE / "THIRD-PARTY-NOTICES.txt").write_text(
        "OpenSeek " + v + ": third-party components\n\nOpenSeek itself is AGPL-3.0-or-later (LICENSE). Complete corresponding source: https://github.com/obadevyh/openseek/tree/" + tag + "\n\n"
        "FFmpeg, x264, x265 and the other native libraries bundled with the server: see SOURCE-OFFER.txt and licenses\\third-party\\SOURCES-MANIFEST.md.\n"
        "WinSW 2.12.0 (service wrapper, MIT): licenses\\WinSW.\n\n--- Desktop app (Qt/PySide6 and others) ---\n"
        + (BUILD / "desktop-notices.txt").read_text(encoding="utf-8") + "\n--- Server Python packages: licence files are in licenses\\server-python-packages ---\n"
        + ", ".join(pkgs) + "\n", encoding="utf-8", newline="\r\n")
    (STAGE / "README.txt").write_text(f"""OpenSeek {v}

OpenSeek is installed and running in the background as the Windows service "OpenSeek" (it starts with Windows and restarts itself if it stops).
Open "OpenSeek" from the Start menu to see the dashboard and press "Connect a TV" to pair a TV on your home network.

 * Your data (cache, paired devices, certificate, settings) is in C:\\ProgramData\\OpenSeek. Only you, Administrators and the service can read it.
 * Home-network access: the installer added one Windows Firewall rule (TCP, this program only, local network only). Nothing is opened to the internet
   and no router settings are changed. The admin interface is never reachable from the network.
 * Settings: the dashboard's Settings page has an "advanced options" switch for ports, bandwidth, cache size, logs and sharing.
 * Command-line options of the installer: /PORT=n /ADMINPORT=n choose the ports (otherwise 8787/8788, or the next free ones), /VERYSILENT for unattended installs.
 * Uninstall from Windows Settings > Apps, or run unins000.exe (add /DELETEDATA=1 to also remove your data; otherwise it is kept).
 * Source code, licences and the written offer for the FFmpeg/x264/x265 sources: see LICENSE, THIRD-PARTY-NOTICES.txt and SOURCE-OFFER.txt.
   Project: https://github.com/obadevyh/openseek
""", encoding="utf-8", newline="\r\n")

    shutil.rmtree(OUT, ignore_errors=True)
    run([iscc, f"/DAppVersion={v}", HERE / "OpenSeek.iss"], cwd=HERE)
    exe = OUT / f"OpenSeekSetup-v{v}-Windows-x64.exe"
    h = sha256(exe)
    (OUT / f"{exe.name}.sha256").write_text(f"{h}  {exe.name}\n", encoding="ascii")
    print(f"\n{exe}  {exe.stat().st_size / 1048576:.1f} MiB\nsha256 {h}")

    if a.sources_archives:
        src = Path(a.sources_archives)
        zp = OUT / f"OpenSeek-v{v}-third-party-sources.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    z.write(f, f"third-party-sources/{f.relative_to(src).as_posix()}")
            z.write(HERE / "third-party" / "SOURCES-MANIFEST.md", "third-party-sources/SOURCES-MANIFEST.md")
            z.write(HERE / "third-party" / "SHA256SUMS-archives.txt", "third-party-sources/SHA256SUMS-archives.txt")
        (OUT / f"{zp.name}.sha256").write_text(f"{sha256(zp)}  {zp.name}\n", encoding="ascii")
        print(f"{zp}  {zp.stat().st_size / 1048576:.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
