"""Copy the licence files of the packages bundled into the Windows build into <out>/<package>/ and write THIRD-PARTY-NOTICES.txt.

    python collect_licenses.py <out-dir> <notices-file>

Reads the installed distributions' metadata (the licence/notice files that ship in each dist-info). Run with the build venv's Python.
"""
from __future__ import annotations

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

BUNDLED = ["PySide6-Essentials", "shiboken6", "httpx", "httpcore", "h11", "anyio", "idna", "certifi", "psutil", "Pillow"]
LICENSE_FILE = re.compile(r"(^|/)(licen[cs]es?|copying|notice|authors)([^/]*)$", re.IGNORECASE)


def main() -> int:
    out, notices = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in BUNDLED:
        d = metadata.distribution(name)
        md = d.metadata
        lic = md.get("License-Expression") or ""
        if not lic:
            cls = [c.split("::")[-1].strip() for c in (md.get_all("Classifier") or []) if c.startswith("License ::")]
            raw = (md.get("License") or "").strip()
            lic = "; ".join(cls) or (raw.splitlines()[0][:80] if raw else "")
        copied = 0
        for f in d.files or []:
            sp = str(f).replace("\\", "/")
            if ".dist-info/" in sp and LICENSE_FILE.search(sp) and not sp.endswith((".py", ".pyc")):
                dest = out / name / Path(sp).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(d.locate_file(f), dest)
                copied += 1
        rows.append((md["Name"], md["Version"], lic or "see licence files", copied))
    lines = [
        "OpenSeek Desktop: third-party components bundled in this build",
        "",
        "OpenSeek itself is licensed under the GNU Affero General Public License v3.0 or later (see LICENSE).",
        "Python itself (PSF License) and the packages below are included by PyInstaller. Each package's own licence files are in the",
        "licenses folder next to this file.",
        "",
        "Qt / PySide6: licensed under the GNU Lesser General Public License v3.0 (and other options offered by The Qt Company). The Qt libraries",
        "are dynamically linked DLLs inside the _internal folder; you may replace them with your own build of the same version. Qt source code is",
        "available from https://download.qt.io/ and PySide6 source from https://code.qt.io/cgit/pyside/pyside-setup.git/.",
        "certifi: Mozilla Public License 2.0 (its CA bundle is used for HTTPS lookups); source at https://github.com/certifi/python-certifi.",
        "",
        f"{'package':24} {'version':14} licence (as declared in the package metadata)",
    ]
    for n, v, l, c in rows:
        lines.append(f"{n:24} {v:14} {l}   [{c} licence file(s) in licenses/]")
    lines += ["", "Not bundled: the OpenSeek server (backend), PyAV/FFmpeg, and anything under desktop/demo data."]
    notices.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"collected {sum(r[3] for r in rows)} licence files for {len(rows)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
