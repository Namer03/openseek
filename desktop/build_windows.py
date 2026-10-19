"""Build the portable Windows release of OpenSeek Desktop (a ZIP with OpenSeek.exe, no Python or PowerShell needed to run it).

    python build_windows.py [--version 0.1.0] [--venv PATH]

Needs 64-bit CPython 3.14 on Windows. Creates (or reuses) a virtual environment with the exact versions in requirements-build.txt, runs
PyInstaller (one-folder, windowed), adds README, LICENSE, THIRD-PARTY-NOTICES and the licence files of the bundled packages, and writes
.build/OpenSeek-Desktop-v<version>-Windows-x64.zip plus its .sha256. Nothing outside desktop/.build is touched.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / ".build"

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, 0), prodvers=({a}, {b}, {c}, 0), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'OpenSeek authors'),
      StringStruct('FileDescription', 'OpenSeek Desktop'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'OpenSeek'),
      StringStruct('LegalCopyright', 'Copyright (C) 2026 the OpenSeek authors. AGPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'OpenSeek.exe'),
      StringStruct('ProductName', 'OpenSeek Desktop'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def run(cmd, **kw):
    print(">", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument("--venv", default=str(BUILD / "venv"))
    a = ap.parse_args()
    v = a.version
    major, minor, patch = (int(x) for x in v.split("."))
    venv = Path(a.venv)
    py = venv / "Scripts" / "python.exe"
    if not py.exists():
        run([sys.executable, "-m", "venv", venv])
        run([py, "-m", "pip", "install", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "-r", HERE / "requirements-build.txt"])

    work = BUILD / "work"
    dist = BUILD / "dist"
    for d in (work, dist):
        shutil.rmtree(d, ignore_errors=True)
    BUILD.mkdir(exist_ok=True)
    vf = BUILD / "version_info.txt"
    vf.write_text(VERSION_INFO.format(a=major, b=minor, c=patch, v=v), encoding="utf-8")
    run([py, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--noupx", "--name", "OpenSeek", "--icon", HERE / "icon.ico",
         "--version-file", vf, "--distpath", dist, "--workpath", work, "--specpath", BUILD, "--paths", HERE,
         "--exclude-module", "tkinter", "--exclude-module", "unittest", HERE / "run_openseek.py"], cwd=HERE)

    name = f"OpenSeek-Desktop-v{v}-Windows-x64"
    stage = BUILD / name
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(dist / "OpenSeek", stage)
    (stage / "README.txt").write_text((HERE / "windows-release" / "README.txt").read_text(encoding="utf-8").replace("{VERSION}", v),
                                      encoding="utf-8", newline="\r\n")
    shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
    run([py, HERE / "collect_licenses.py", stage / "licenses", stage / "THIRD-PARTY-NOTICES.txt"])

    zp = BUILD / f"{name}.zip"
    zp.unlink(missing_ok=True)
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                z.write(f, f"{name}/{f.relative_to(stage).as_posix()}")
    h = hashlib.sha256(zp.read_bytes()).hexdigest()
    (BUILD / f"{zp.name}.sha256").write_text(f"{h}  {zp.name}\n", encoding="ascii")
    print(f"\n{zp}  {zp.stat().st_size / 1048576:.1f} MiB\nsha256 {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
