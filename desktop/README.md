# OpenSeek Desktop

Native Windows app (PySide6) for an existing OpenSeek server. It attaches to the running server (never starts a second one on a busy port),
reads the cache database read-only, and uses the loopback admin API for controls.

* Build: `python -m venv .venv && .venv\Scripts\pip install PySide6 pyinstaller httpx psutil Pillow`, then
  `.venv\Scripts\python -m PyInstaller --noconfirm --clean --noconsole --name OpenSeek --icon icon.ico run_openseek.py`
* Run from source: `.venv\Scripts\python run_openseek.py`
* Packaging check: `OpenSeek.exe --selftest <folder>` renders every page to PNG and exits.
* Identify / delete one cache entry use `PUT|DELETE /admin/library/entries/{fp}` (needs the server code from this branch).
* No credentials are stored. Metadata comes from Cinemeta (no key); exports redact tokens, URL paths and ids.
