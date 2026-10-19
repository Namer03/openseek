"""Demo mode for OpenSeek Desktop: runs the real UI against synthetic, fully offline sample data.

    python run_openseek.py --demo                 # open the app on sample data
    python run_openseek.py --demo --shots out_dir # render the pages to PNG files and exit

Safety:
* All data lives in a fresh temporary folder created here (marked with DEMO_MARKER). Every read path is guarded by
  `assert_demo_dir`, which refuses any folder that is not such a marker-carrying folder under the system temp dir.
* The admin API, the health check, process control and Cinemeta are replaced by in-memory fakes: no socket is opened,
  no real server or data folder is read, started, stopped or modified, and the saved app configuration is never touched.
* Titles are Blender Foundation open movies (CC BY). Posters and thumbnails are colour gradients with text drawn locally
  with Pillow. No film frames, no network.
"""
from __future__ import annotations

import hashlib
import random
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

DEMO_MARKER = ".openseek-demo-data"

# (title, year, imdb-like synthetic id, runtime seconds, tiles done / total, colours, description)
TITLES = [
    ("Big Buck Bunny", "2008", "tt9000001", 596, 1.0, ("#2f7d4f", "#e3b04b"),
     "A giant rabbit takes revenge on three bullying rodents. Blender Foundation open movie (CC BY)."),
    ("Sintel", "2010", "tt9000002", 888, 0.62, ("#7a3b8f", "#e0603f"),
     "A young woman searches for her lost dragon. Blender Foundation open movie (CC BY)."),
    ("Tears of Steel", "2012", "tt9000003", 734, 1.0, ("#1f4f8f", "#e8873a"),
     "Scientists try to save the world from killer robots. Blender Foundation open movie (CC BY)."),
    ("Elephants Dream", "2006", "tt9000004", 654, 1.0, ("#8f3a2f", "#3aa5b0"),
     "Two characters explore a strange machine world. Blender Foundation open movie (CC BY)."),
    ("Cosmos Laundromat", "2015", "tt9000005", 726, 0.27, ("#c0517a", "#2e8b9c"),
     "A suicidal sheep is offered a new life. Blender Foundation open movie (CC BY)."),
]
TILE_STEP_S = 10
TILE_BYTES = 380_000


def fp_of(name: str) -> str:
    return hashlib.sha256(("demo:" + name).encode()).hexdigest()


def assert_demo_dir(path) -> Path:
    p = Path(path).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if not (p.is_dir() and (p / DEMO_MARKER).is_file() and (tmp == p or tmp in p.parents)):
        raise RuntimeError(f"Demo mode refuses to use {p}: it is not a demo data folder created by --demo.")
    return p


# ------------------------------------------------------------------------------------------------ synthetic images
def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    for name in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _hex(c: str):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def gradient(w: int, h: int, c1, c2, shift: float = 0.0):
    from PIL import Image
    a, b = _hex(c1), _hex(c2)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = min(1.0, max(0.0, (x / w * 0.45 + y / h * 0.55) + shift))
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return img


def make_poster(path: Path, title: str, year: str, cols) -> None:
    from PIL import ImageDraw
    w, h = 352, 464
    img = gradient(w, h, *cols)
    d = ImageDraw.Draw(img)
    d.ellipse((w * 0.45, -h * 0.1, w * 1.3, h * 0.45), fill=tuple(min(255, v + 40) for v in _hex(cols[1])))
    words, lines, cur = title.split(), [], ""
    f = _font(46)
    for wd in words:
        t = (cur + " " + wd).strip()
        if d.textlength(t, font=f) > w - 50 and cur:
            lines.append(cur)
            cur = wd
        else:
            cur = t
    lines.append(cur)
    y = h * 0.52
    for ln in lines:
        d.text((26, y), ln, font=f, fill="white")
        y += 54
    d.text((28, y + 8), year + "  ·  sample poster", font=_font(20, False), fill=(255, 255, 255))
    img.save(path, "JPEG", quality=88)


def make_tiles(folder: Path, n: int, cols, label: str) -> None:
    from PIL import ImageDraw
    folder.mkdir(parents=True, exist_ok=True)
    f = _font(26)
    for i in range(n):
        img = gradient(256, 144, *cols, shift=-0.25 + 0.5 * i / max(1, n - 1))
        d = ImageDraw.Draw(img)
        d.text((12, 100), f"{i * TILE_STEP_S // 60}:{i * TILE_STEP_S % 60:02d}", font=f, fill="white")
        img.save(folder / f"{i}.jpg", "JPEG", quality=70)


# ------------------------------------------------------------------------------------------------ data folder
def build_data_dir() -> Path:
    root = Path(tempfile.mkdtemp(prefix="openseek-demo-"))
    (root / DEMO_MARKER).write_text("synthetic OpenSeek Desktop demo data; safe to delete\n", encoding="utf-8")
    now = time.time()
    con = sqlite3.connect(root / "openseek.db")
    con.executescript("""
        create table entries (fp text primary key, duration_ms int, n_tiles int, bytes int, created real, last_used real);
        create table tiles (fp text, idx int);
        create table release_meta (fp text primary key, imdb_id text, tmdb_id int, season int, episode int);
    """)
    rnd = random.Random(7)
    for k, (title, year, imdb, secs, frac, cols, _d) in enumerate(TITLES):
        fp = fp_of(title)
        total = secs // TILE_STEP_S
        done = int(total * frac)
        make_tiles(root / "cache" / fp[:2] / fp / "v1" / "tiles", done, cols, title)
        make_poster(root / f"poster-{imdb}.jpg", title, year, cols)
        con.execute("insert into entries values (?,?,?,?,?,?)",
                    (fp, secs * 1000, total, done * TILE_BYTES, now - 86400 * (9 - k), now - 3600 * (k * 7 + 2)))
        con.executemany("insert into tiles values (?,?)", [(fp, i) for i in range(done)])
        con.execute("insert into release_meta values (?,?,?,?,?)", (fp, imdb, None, 0, 0))
    # one cache entry that was never identified
    fp = fp_of("untagged")
    cols = ("#3b4252", "#81a1c1")
    make_tiles(root / "cache" / fp[:2] / fp / "v1" / "tiles", 40, cols, "")
    con.execute("insert into entries values (?,?,?,?,?,?)", (fp, 400_000, 40, 40 * TILE_BYTES, now - 86400 * 3, now - 86400 * 2))
    con.executemany("insert into tiles values (?,?)", [(fp, i) for i in range(40)])
    con.commit()
    con.close()
    # synthetic server log (timestamps relative to now)
    logs = root / "logs"
    logs.mkdir()
    ev = [
        (-3300, "INFO", "openseek", "OpenSeek started (demo data)"),
        (-3250, "INFO", "openseek.share", "pair/claim 200"),
        (-2900, "INFO", "openseek.api", "preflight ok source=a41c9e02 duration=888s"),
        (-2880, "INFO", "openseek.extract", "source a41c9e02 started: 88 thumbnails planned"),
        (-2400, "INFO", "openseek.share", "/v1/lookup 200"),
        (-1800, "WARNING", "openseek.guard", "pausing background extraction while playing"),
        (-1500, "INFO", "openseek.guard", "resuming background extraction"),
        (-1200, "INFO", "openseek.cache", "evicted 2 old cache entries (retention)"),
        (-900, "INFO", "openseek.extract", "source 5be0d371 stopped (done)"),
        (-600, "INFO", "openseek.share", "/v1/lookup 200"),
        (-300, "INFO", "openseek.api", "preflight ok source=c77a10f4 duration=726s"),
        (-120, "INFO", "openseek.extract", "source c77a10f4 progress 20/72"),
        (-30, "INFO", "openseek.extract", "source a41c9e02 progress 55/88"),
    ]
    lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now + dt))},{rnd.randint(100, 999)} {lv} {lg}: {m}" for dt, lv, lg, m in ev]
    (logs / "serve-demo.err.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


# ------------------------------------------------------------------------------------------------ fake server state
class FakeServer:
    def __init__(self, data_dir: Path):
        now = time.time()
        self.data_dir = data_dir
        self.sharing = {"enabled": True, "metadata_index": True,
                        "public": {"listening": True, "host": "openseek.example.org", "port": 9443, "blocked": 0, "refused_total": 12}}
        self.contrib = {"enabled": True, "effective": True, "mbps": 8, "hosts": ["media.example.org", "cdn.example.net"],
                        "denied_hosts": [], "jobs": [{"device_id": "demo-guest-laptop", "preview_id": "p1", "state": "extracting", "done": 31, "total": 72}]}
        self.devices = [
            {"device_id": "demo-owner-tv", "name": "Living-room TV", "role": "owner", "contribute": False, "created": now - 86400 * 40, "last_seen": now - 240, "expires": 0},
            {"device_id": "demo-guest-laptop", "name": "Guest laptop", "role": "guest", "contribute": True, "created": now - 86400 * 6, "last_seen": now - 60, "expires": now + 86400 * 24},
            {"device_id": "demo-guest-phone", "name": "Guest phone", "role": "guest", "contribute": False, "created": now - 86400 * 2, "last_seen": now - 3 * 3600, "expires": now + 86400 * 28},
        ]
        self.invites = {"invites": [
            {"label": "Alex (laptop)", "state": "used", "expires_in_s": 0, "attempts_left": 5},
            {"label": "Sam (phone)", "state": "used", "expires_in_s": 0, "attempts_left": 5},
            {"label": "Weekend guest", "state": "pending", "expires_in_s": 20 * 3600, "attempts_left": 5},
        ]}
        self.t0 = now

    def status(self):
        return {"cache": {"max_bytes": 2 * 1024 ** 3}, "active_sources": 2, "devices": self.devices}

    def metrics(self):
        now = time.time()
        ev = [{"at": now - 55 + i * 5, "bytes": 1_600_000, "requests": 3} for i in range(12)]
        sintel, cosmos = fp_of("Sintel"), fp_of("Cosmos Laundromat")
        done_src = fp_of("Big Buck Bunny")
        return {"config": {"playback_guard": True, "multirange_prefetch": True, "workers_per_source": 2, "upstream_rps": 4, "retention_days": 60,
                           "bg_mbps": 8, "pregen": 12, "tolerance_s": 5, "multirange_batch": 8, "alt_source": True, "max_active_sources": 2},
                "sources": [
                    {"source": sintel[:16], "running": True, "done": 55, "total": 88, "events": ev, "cov_60s": 0.58},
                    {"source": cosmos[:16], "running": True, "done": 20, "total": 72, "events": ev[:6], "cov_60s": 0.31, "contributed": True},
                    {"source": done_src[:16], "running": False, "done": 59, "total": 59, "events": ev[:2], "cov_60s": 1.0},
                ], "failed_jobs": []}

    def call(self, method: str, path: str, body=None):
        if method == "GET":
            if path == "/admin/status": return self.status()
            if path == "/admin/metrics": return self.metrics()
            if path == "/admin/sharing": return self.sharing
            if path == "/admin/contributions": return self.contrib
            if path == "/admin/devices": return {"devices": self.devices}
            if path == "/admin/invites": return self.invites
            return {}
        if method == "PUT" and path == "/admin/sharing":
            self.sharing.update(body or {})
        elif method == "PUT" and path == "/admin/contributions":
            self.contrib.update(body or {})
        elif method == "DELETE" and path.startswith("/admin/devices/"):
            self.devices = [d for d in self.devices if d["device_id"] != path.rsplit("/", 1)[-1]]
        return {}          # demo: other actions are accepted and ignored


# ------------------------------------------------------------------------------------------------ installation
def install(shots_dir: str | None = None) -> Path:
    """Replace every data source with the offline fakes. Returns the temporary demo data folder."""
    from . import core, ui
    root = build_data_dir()
    srv = FakeServer(root)
    posters = {t[2]: str(root / f"poster-{t[2]}.jpg") for t in TITLES}
    metas = {t[2]: {"id": t[2], "name": t[0], "releaseInfo": t[1], "description": t[6], "_type": "movie"} for t in TITLES}

    cfg = core.Config(data_dir=str(root), backend_dir="", api_port=48787, admin_port=48788, tray=False, public_host="openseek.example.org")

    core.Config.load = classmethod(lambda cls: cfg)
    core.Config.save = lambda self: None
    core.install_info = lambda: None                          # never read a real installation's state
    core.is_installed = lambda: False
    core.Admin.call = lambda self, method, path, json_body=None: srv.call(method, path, json_body)
    core.api_health = lambda c: {"service": "openseek", "demo": True}
    core.server_processes = lambda c: [object(), object()]
    core.start_server = lambda c, public_host="": (False, "Demo mode: no server is started.")
    core.stop_server = lambda c: (False, "Demo mode: no server is stopped.")

    orig_entries, orig_tail = core.Library.entries, core.LogTail.read_new
    def guarded_entries(self):
        assert_demo_dir(self.cfg.data_dir)
        return orig_entries(self)
    def guarded_tail(self, first_bytes=400_000):
        assert_demo_dir(self.cfg.data_dir)
        return orig_tail(self, first_bytes)
    core.Library.entries, core.LogTail.read_new = guarded_entries, guarded_tail

    def meta_init(self):
        self.dir = root / "meta"
    core.Metadata.__init__ = meta_init
    core.Metadata.get = lambda self, imdb, hint_series: metas.get(imdb)
    core.Metadata.poster_path = lambda self, m: Path(posters[m["id"]]) if m and m.get("id") in posters else None
    core.Metadata.search = staticmethod(lambda q: [{"id": t[2], "name": t[0], "year": t[1], "type": "movie"} for t in TITLES if q.lower() in t[0].lower()])

    ui.set_autostart = lambda on: None
    ui.autostart_enabled = lambda: False
    import atexit
    atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
    return root


def run_shots(out: str, size=(1280, 800)) -> None:
    """Render the main pages (plus one title detail) of the demo to PNG files."""
    import sys
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import QApplication
    from . import theme
    from .ui import MainWindow, make_icon
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.QSS)
    app.setFont(QFont("Segoe UI Variable Text", 10))
    from .core import Config
    w = MainWindow(Config.load())
    w.cfg.tray = False
    w.resize(*size)
    w.show()
    # show neutral display paths instead of the temporary folder (display only; the real demo folder is unchanged)
    w.sett.data.setText(r"C:\OpenSeek\data")
    w.sett.backend.setText(r"C:\OpenSeek\backend")

    def spin(ms):
        end = time.time() + ms / 1000
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)
    spin(3500)
    for name, i in (("dashboard", 0), ("library", 1), ("activity", 2), ("logs", 3), ("sharing", 4), ("settings", 5)):
        w.resize(1440 if name == "logs" else size[0], 980 if name == "sharing" else size[1])
        w.go(i)
        spin(700)
        w.grab().save(str(outp / f"desktop-{name}.png"))
    cards = [c for c in w.cards if c.title == "Sintel"]
    if cards:
        w.open_card(cards[0])
        spin(900)
        w.grab().save(str(outp / "desktop-detail.png"))
    w.quit_app()
