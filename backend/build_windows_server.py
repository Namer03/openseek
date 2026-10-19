r"""Build the frozen Windows OpenSeek server (PyInstaller one-folder, CONSOLE subsystem so a service wrapper can capture stdout/stderr).

    python build_windows_server.py [--out DIR] [--venv PATH]

Needs 64-bit CPython 3.14 on Windows. Creates (or reuses) a virtual environment holding exactly the pins of requirements.txt +
requirements-lock.txt plus the build tools of requirements-build-windows.txt, and runs PyInstaller on run_server.py.
Output: <out>/dist/OpenSeekServer/OpenSeekServer.exe (default out: <base>\server).
The same exe is the extraction worker (`OpenSeekServer.exe --openseek-worker`, see run_server.py).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASE = HERE / ".build-windows-server"

# Modules PyInstaller's hooks would otherwise pull in, none of which the server uses.
EXCLUDES = ["tkinter", "unittest", "pytest", "_pytest", "httpx", "psutil", "pydoc", "test", "lib2to3", "setuptools", "pip",
            "distutils", "IPython", "matplotlib", "scipy", "pandas", "PIL.ImageTk", "PIL._tkinter_finder"]
# Imported by name at run time (uvicorn picks protocol/loop/lifespan implementations from strings).
HIDDEN = ["uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.websockets_impl", "uvicorn.protocols.http.auto",
          "uvicorn.lifespan.on", "uvicorn.lifespan.off", "uvicorn.lifespan.auto", "uvicorn.loops.asyncio", "uvicorn.loops.auto",
          "h11", "anyio._backends._asyncio", "cryptography", "cryptography.hazmat.bindings._rust",
          "openseek.worker", "openseek.engine", "openseek.altpipeline", "openseek.altsource"]
VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, 0), prodvers=({a}, {b}, {c}, 0), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'OpenSeek authors'),
      StringStruct('FileDescription', 'OpenSeek Server'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'OpenSeekServer'),
      StringStruct('LegalCopyright', 'Copyright (C) 2026 the OpenSeek authors. AGPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'OpenSeekServer.exe'),
      StringStruct('ProductName', 'OpenSeek Server'),
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
    ap.add_argument("--out", default=str(DEFAULT_BASE / "server"))
    ap.add_argument("--venv", default=str(DEFAULT_BASE / "venv"))
    a = ap.parse_args()
    out, venv = Path(a.out), Path(a.venv)
    py = venv / "Scripts" / "python.exe"
    if not py.exists():
        run([sys.executable, "-m", "venv", venv])
        run([py, "-m", "pip", "install", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "-r", HERE / "requirements.txt", "-r", HERE / "requirements-build-windows.txt",
             "-c", HERE / "requirements-lock.txt"])
    version = re.search(r'__version__\s*=\s*"([^"]+)"', (HERE / "openseek" / "__init__.py").read_text(encoding="utf-8")).group(1)
    major, minor, patch = (int(x) for x in version.split("."))
    work, dist = out / "work", out / "dist"
    out.mkdir(parents=True, exist_ok=True)
    vf = out / "version_info.txt"
    vf.write_text(VERSION_INFO.format(a=major, b=minor, c=patch, v=version), encoding="utf-8")
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--console", "--noupx", "--name", "OpenSeekServer",
           "--version-file", vf, "--distpath", dist, "--workpath", work, "--specpath", out, "--paths", HERE,
           "--collect-all", "av", "--collect-submodules", "uvicorn", "--collect-submodules", "anyio",
           "--collect-submodules", "PIL"]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    run(cmd + [HERE / "run_server.py"], cwd=HERE)
    exe = dist / "OpenSeekServer" / "OpenSeekServer.exe"
    size = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
    print(f"\n{exe}  bundle {size / 1048576:.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
