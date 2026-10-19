"""Data access for OpenSeek Desktop. Everything here is READ-ONLY against the live server's files (SQLite opened with
mode=ro, JPEG files read from the cache directory) except the few explicit owner actions that go through the loopback
admin API (identify an entry, delete one entry, sharing, invitations, devices). Nothing here writes to the database
or the cache directly, and no credential is ever stored or logged."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

APP = "OpenSeekDesktop"
APPDATA = Path(os.environ.get("APPDATA", str(Path.home()))) / APP
APPDATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APPDATA / "config.json"


def admin_headers(data_dir: str) -> dict:
    """The server's per-install admin secret: OPENSEEK_ADMIN_TOKEN, else <data>/admin.token (owner-only file)."""
    tok = os.environ.get("OPENSEEK_ADMIN_TOKEN", "")
    if not tok:
        try:
            tok = (Path(data_dir) / "admin.token").read_bytes().hex()
        except OSError:
            tok = ""
    return {"X-OpenSeek-Admin": tok}


# --------------------------------------------------------------------------------------------- configuration
def _guess_data_dir() -> str:
    env = os.environ.get("OPENSEEK_DATA_DIR")
    default = str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "OpenSeek" / "data")
    for c in (env, default, str(Path.home() / "openseek-data")):
        if c and (Path(c) / "openseek.db").exists():
            return c
    return env or default


def _guess_backend_dir() -> str:
    for c in (os.environ.get("OPENSEEK_BACKEND_DIR"), str(Path(__file__).resolve().parents[2] / "backend"),
              str(Path.home() / "openseek-backend" / "backend")):
        if c and (Path(c) / "tools" / "run_production.ps1").exists():
            return c
    return ""


@dataclass
class Config:
    data_dir: str = field(default_factory=_guess_data_dir)
    backend_dir: str = field(default_factory=_guess_backend_dir)
    api_port: int = 8787
    admin_port: int = 8788
    tray: bool = True
    public_host: str = ""

    @classmethod
    def load(cls) -> "Config":
        c = cls()
        try:
            for k, v in json.loads(CONFIG_PATH.read_text(encoding="utf-8")).items():
                if hasattr(c, k):
                    setattr(c, k, v)
        except (OSError, ValueError):
            pass
        return c

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------------------------- admin API / server
class Admin:
    """Loopback admin API of the running server. Every call returns None when the server is not reachable."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.auth_failed = False        # the server answered but refused the admin secret (old desktop build, wrong data folder, other user)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.cfg.admin_port}", headers=admin_headers(self.cfg.data_dir), timeout=4.0)

    def call(self, method: str, path: str, json_body=None):
        try:
            with self._client() as c:
                r = c.request(method, path, json=json_body)
            if r.status_code == 204:
                return {}
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code == 403 and body.get("error") == "admin_auth":
                self.auth_failed = True
                return None                 # treated like "admin unreachable"; the window explains why
            self.auth_failed = False
            return body if r.status_code < 400 else {"_error": body.get("error", str(r.status_code)), "_status": r.status_code}
        except httpx.HTTPError:
            return None

    def get(self, path):
        return self.call("GET", path)


def api_health(cfg: Config) -> dict | None:
    """The owner API (HTTPS, self-signed): is an OpenSeek answering on 8787?"""
    try:
        with httpx.Client(verify=False, timeout=2.5) as c:
            r = c.get(f"https://127.0.0.1:{cfg.api_port}/v1/health")
        j = r.json()
        return j if j.get("service") == "openseek" else None
    except (httpx.HTTPError, ValueError):
        return None


def _is_openseek_serve(p: psutil.Process) -> bool:
    try:
        cl = " ".join(p.cmdline()).lower()
    except (psutil.Error, OSError):
        return False
    return "openseek" in cl and " serve" in cl and "python" in cl


def server_processes(cfg: Config) -> list[psutil.Process]:
    """The OpenSeek serve process(es): the one listening on the API port and its launcher parent. Nothing else, ever."""
    out: list[psutil.Process] = []
    try:
        conns = [c for c in psutil.net_connections("tcp") if c.status == psutil.CONN_LISTEN and c.laddr.port == cfg.api_port and c.pid]
    except (psutil.Error, OSError):
        conns = []
    for c in conns:
        try:
            p = psutil.Process(c.pid)
        except psutil.Error:
            continue
        if _is_openseek_serve(p) and p not in out:
            out.append(p)
            par = p.parent()
            if par is not None and _is_openseek_serve(par) and par not in out:
                out.append(par)
    return out


def start_server(cfg: Config, public_host: str = "") -> tuple[bool, str]:
    """Start through the existing run_production.ps1 (same validated settings), never a second server on a busy port."""
    if api_health(cfg) is not None:
        return False, "OpenSeek is already running on port %d." % cfg.api_port
    script = Path(cfg.backend_dir) / "tools" / "run_production.ps1"
    if not script.exists():
        return False, "run_production.ps1 not found. Set the backend folder in Settings."
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    if public_host:
        cmd += ["-PublicHost", public_host]
    subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "Starting OpenSeek..."


def stop_server(cfg: Config) -> tuple[bool, str]:
    procs = server_processes(cfg)
    if not procs:
        return False, "No OpenSeek server process was found on port %d (nothing was touched)." % cfg.api_port
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=6)
    for p in procs:
        try:
            if p.is_running():
                p.kill()
        except psutil.Error:
            pass
    return True, "OpenSeek stopped."


# --------------------------------------------------------------------------------------------- library (read-only)
@dataclass
class Entry:
    fp: str
    duration_ms: int
    n_tiles: int
    bytes: int
    created: float
    last_used: float
    tiles: int
    imdb: str | None = None
    tmdb: int | None = None
    season: int = 0
    episode: int = 0

    @property
    def complete(self) -> bool:
        return self.n_tiles > 0 and self.tiles >= self.n_tiles

    @property
    def identified(self) -> bool:
        return bool(self.imdb or self.tmdb)

    @property
    def is_episode(self) -> bool:
        return self.identified and (self.season > 0 or self.episode > 0)


class Library:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def db_path(self) -> Path:
        return Path(self.cfg.data_dir) / "openseek.db"

    def entries(self) -> list[Entry]:
        if not self.db_path.exists():
            return []
        con = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            tabs = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            meta = "release_meta" in tabs
            q = ("select e.fp,e.duration_ms,e.n_tiles,e.bytes,e.created,e.last_used,"
                 "(select count(*) from tiles t where t.fp=e.fp)" +
                 (",m.imdb_id,m.tmdb_id,m.season,m.episode from entries e left join release_meta m on m.fp=e.fp"
                  if meta else " from entries e"))
            out = []
            for r in con.execute(q):
                e = Entry(*r[:7])
                if meta:
                    e.imdb, e.tmdb, e.season, e.episode = r[7], r[8], r[9] or 0, r[10] or 0
                out.append(e)
            return out
        finally:
            con.close()

    def entry_dir(self, fp: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{64}", fp):
            return None
        base = Path(self.cfg.data_dir) / "cache" / fp[:2] / fp
        subs = [d for d in base.glob("*") if (d / "tiles").is_dir()] if base.is_dir() else []
        return subs[0] if subs else None

    def tile_files(self, fp: str) -> list[tuple[int, Path]]:
        d = self.entry_dir(fp)
        if d is None:
            return []
        out = []
        for p in (d / "tiles").glob("*.jpg"):
            if p.stem.isdigit():
                out.append((int(p.stem), p))
        return sorted(out)

    def poster_frame(self, e: Entry) -> Path | None:
        """A REAL cached frame (about a quarter of the way in) to represent a title that has no poster."""
        files = self.tile_files(e.fp)
        if not files:
            return None
        return files[len(files) // 4][1]


# --------------------------------------------------------------------------------------------- metadata (Cinemeta, no key)
class Metadata:
    """Titles, posters, years and episode names from Cinemeta (Stremio's public catalogue), cached on disk. Missing data is
    simply missing: nothing is invented."""

    def __init__(self):
        self.dir = APPDATA / "meta"
        self.dir.mkdir(exist_ok=True)

    def _file(self, key: str) -> Path:
        return self.dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")

    def get(self, imdb: str, hint_series: bool) -> dict | None:
        f = self._file(imdb)
        if f.exists() and time.time() - f.stat().st_mtime < 30 * 86400:
            try:
                return json.loads(f.read_text(encoding="utf-8")) or None
            except ValueError:
                pass
        for typ in (("series", "movie") if hint_series else ("movie", "series")):
            try:
                r = httpx.get(f"https://v3-cinemeta.strem.io/meta/{typ}/{imdb}.json", timeout=8)
                m = r.json().get("meta") if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                return None                                   # offline: try again later, cache nothing
            if m and m.get("name"):
                m["_type"] = typ
                f.write_text(json.dumps(m), encoding="utf-8")
                return m
        f.write_text("null", encoding="utf-8")
        return None

    def poster_path(self, m: dict) -> Path | None:
        url = m.get("poster")
        if not url or not url.startswith("https://"):
            return None
        f = self.dir / (re.sub(r"[^A-Za-z0-9]", "_", m.get("id", "x")) + ".jpg")
        if f.exists() and f.stat().st_size > 1000:
            return f
        try:
            r = httpx.get(url, timeout=12)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                f.write_bytes(r.content)
                return f
        except httpx.HTTPError:
            pass
        return None

    @staticmethod
    def search(query: str) -> list[dict]:
        out = []
        for typ in ("movie", "series"):
            try:
                r = httpx.get(f"https://v3-cinemeta.strem.io/catalog/{typ}/top/search={httpx.QueryParams({'q': query})['q']}.json", timeout=8)
                for m in (r.json().get("metas") or [])[:6]:
                    if m.get("id", "").startswith("tt"):
                        out.append({"id": m["id"], "name": m.get("name", "?"), "year": m.get("releaseInfo", ""), "type": typ})
            except (httpx.HTTPError, ValueError):
                pass
        return out

    @staticmethod
    def episode_name(m: dict | None, season: int, episode: int) -> str:
        for v in (m or {}).get("videos", []) or []:
            if v.get("season") == season and v.get("episode") == episode:
                return v.get("name") or v.get("title") or ""
        return ""


# --------------------------------------------------------------------------------------------- logs
LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) ([\w.]+): (.*)$")


@dataclass
class LogLine:
    ts: str
    level: str
    logger: str
    msg: str
    raw: str


class LogTail:
    """Reads the newest OpenSeek serve log (the files the server already writes), incrementally."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path: Path | None = None
        self.pos = 0

    def latest(self) -> Path | None:
        d = Path(self.cfg.data_dir) / "logs"
        files = sorted(d.glob("serve-*.err.log"), key=lambda p: p.stat().st_mtime) if d.is_dir() else []
        return files[-1] if files else None

    def read_new(self, first_bytes: int = 400_000) -> list[LogLine]:
        p = self.latest()
        if p is None:
            return []
        try:
            size = p.stat().st_size
            if p != self.path:
                self.path, self.pos = p, max(0, size - first_bytes)
            if size < self.pos:
                self.pos = 0
            if size == self.pos:
                return []
            with open(p, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
            self.pos = size
        except OSError:
            return []
        out = []
        for raw in data.decode("utf-8", "replace").splitlines():
            m = LOG_RE.match(raw)
            out.append(LogLine(m.group(1), m.group(2), m.group(3), m.group(4), raw) if m else LogLine("", "INFO", "", raw, raw))
        return out


_REDACT = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(https?://[^\s/?#]+)[^\s]*"), r"\1/[path redacted]"),
    (re.compile(r"(?i)(token|secret|code|invite|key|password)(\"?\s*[:=]\s*\"?)[A-Za-z0-9._~+/=-]{6,}"), r"\1\2[redacted]"),
    (re.compile(r"\b[0-9a-f]{32,64}\b"), "[hex]"),
    (re.compile(r"openseek://\S+"), "openseek://[redacted]"),
]


def redact(text: str) -> str:
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


# --------------------------------------------------------------------------------------------- formatting
def human_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return "?"


def human_duration(ms: int) -> str:
    s = int(ms // 1000)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def ago(ts: float) -> str:
    d = time.time() - ts
    if d < 90:
        return "just now"
    for lim, div, unit in ((3600, 60, "min"), (86400, 3600, "h"), (86400 * 60, 86400, "d")):
        if d < lim:
            return f"{int(d // div)} {unit} ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))
