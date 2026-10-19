# OpenSeek Desktop

Native Windows app (PySide6) for an existing OpenSeek server. It attaches to the running server (never starts a second one on a busy port),
reads the cache database read-only, and uses the loopback admin API for controls.

* Build the portable Windows ZIP (needs 64-bit Python 3.14): `python desktop/build_windows.py --version 0.1.0` -> `desktop/.build/OpenSeek-Desktop-v0.1.0-Windows-x64.zip` and a `.sha256`.
  It uses the exact versions in `requirements-build.txt` and PyInstaller in one-folder mode. Ready-made builds are on the GitHub releases page.
* Run from source: `.venv\Scripts\python run_openseek.py`
* Packaging check: `OpenSeek.exe --selftest <folder>` renders every page to PNG and exits.
* Identify / delete one cache entry use `PUT|DELETE /admin/library/entries/{fp}` (needs the server code from this branch).
* No credentials are stored. Metadata comes from Cinemeta (no key); exports redact tokens, URL paths and ids.
