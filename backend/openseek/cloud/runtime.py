"""The cloud runtime: devices, pairing, previews and request-driven extraction.

Per process (one serverless instance or one container) it keeps the loopback range proxy and, per source, a
vetted `Upstream` plus an extraction engine, reusing the self-hosted building blocks unchanged:

    client request -> Cloud -> Upstream (SSRF-vetted range reads) -> RangeProxy (127.0.0.1) -> worker subprocess
                                                                                      (PyAV, no secrets, no URL)

Everything that must survive between requests (which may land on different instances) is in the key-value
store; tiles are in object storage. Work is done in bounded slices: a status poll or a priority call takes a
per-source lock, extracts for at most a few seconds in priority order (scrub target, playhead window,
overview), and returns. Only one frame is ever in flight per source, across all instances.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__, worker as worker_mod
from ..cache import PROFILE, jpeg_ok
from ..fetch import Budget, ProxySource, RangeProxy, Upstream, UpstreamError
from ..ids import is_device_id, is_preview_id
from ..netpolicy import Denied, DestinationPolicy
from ..pairing import (ITERATIONS, MAX_ATTEMPTS, client_proof, derive_key, display_code, new_code,
                       server_proof, valid_device_name)
from ..vtt import render
from ..worker import WorkerEngine, WorkerError
from .crypto import Keys
from .kv import KV, KVError, make_kv
from .settings import CloudSettings
from .storage import Storage, StorageError, make_storage

log = logging.getLogger("openseek.cloud")

P = "os:"                         # key prefix: the store may be shared with other apps
PREVIEW_TTL_S = 6 * 3600
FAIL_TTL_S = 15 * 60
PAIR_WINDOW_S = 600
CLOUD_PIN = "ca"                  # pair/start answers this instead of a certificate pin: the platform's normal certificate is used
FATAL_UPSTREAM = {"source_expired", "budget_exceeded", "range_unsupported", "source_forbidden"}
P0_SPAN = 6


class CloudError(Exception):
    def __init__(self, code: str, status: int = 400, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


# ============================================================================ extraction engines
def _site_dirs() -> list[str]:
    """Directories that hold the decoder's packages, for the isolated (-I) worker child."""
    out: list[str] = []
    for mod in ("av", "numpy", "PIL"):
        spec = importlib.util.find_spec(mod)
        if spec and spec.submodule_search_locations:
            d = str(Path(list(spec.submodule_search_locations)[0]).parent)
            if d not in out:
                out.append(d)
    return out


class InProcessEngine:
    """Fallback when the platform cannot start a child process: the same Extractor, in this process, reading
    through the same loopback proxy. Loses the process boundary between the decoder and the app's secrets."""

    def __init__(self, proxy_url: str, size: int, fmt: str, open_timeout_s: float, frame_timeout_s: float,
                 max_dim: int = 8192, max_duration_s: float = 12 * 3600, tolerance_ms: int = 5000):
        self.proxy_url, self.size, self.fmt = proxy_url, size, fmt
        self.frame_timeout_s = frame_timeout_s
        self.tolerance_ms = tolerance_ms
        self.ex = None

    def is_open(self) -> bool:
        return self.ex is not None

    def open(self):
        from ..engine import EngineError, Extractor, ProxyReader
        from ..worker import OpenResult

        try:
            self.ex = Extractor(ProxyReader(self.proxy_url, self.size), self.fmt)
        except EngineError as e:
            raise WorkerError(e.code, e.detail) from None
        i = self.ex.info()
        return OpenResult(i.duration_ms, i.width, i.height, i.codec, i.hdr)

    def grab(self, t_ms: int, accurate: bool, tolerance_ms: int | None = None):
        from ..engine import EngineError
        from ..worker import GrabResult

        if self.ex is None:
            raise WorkerError("crash", "not_open", transient=True)
        try:
            pts, jpg = self.ex.grab(int(t_ms), bool(accurate),
                                    tolerance_ms=self.tolerance_ms if tolerance_ms is None else tolerance_ms)
        except EngineError as e:
            raise WorkerError(e.code, e.detail) from None
        except Exception as e:  # noqa: BLE001
            raise WorkerError("internal", type(e).__name__) from None
        return GrabResult(pts, jpg)

    def kill(self):
        self.close()

    def close(self):
        ex, self.ex = self.ex, None
        if ex is not None:
            try:
                ex.close()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class Handle:
    fp: str
    upstream: Upstream
    psrc: ProxySource
    cap: str
    size: int
    fmt: str
    engine: object = None
    info: object = None
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    dead: bool = False


# ============================================================================ the runtime
class Cloud:
    def __init__(self, settings: CloudSettings, kv: KV | None = None, storage: Storage | None = None,
                 policy: DestinationPolicy | None = None, engine: str | None = None, clock=time.time):
        self.s = settings
        self.kv = kv if kv is not None else make_kv(settings)
        self.store = storage if storage is not None else make_storage(settings)
        self.keys = Keys(settings.secret)
        self.policy = policy or DestinationPolicy(settings.allowed_ports, settings.allow_http_sources)
        self.clock = clock
        self.engine_mode = (engine or os.environ.get("OPENSEEK_CLOUD_ENGINE") or "subprocess").strip().lower()
        self._proxy: RangeProxy | None = None
        self._handles: dict[str, Handle] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        self._recent: OrderedDict[str, bytes] = OrderedDict()     # just-made tiles, saves a storage round trip
        self._dev_seen: dict[str, float] = {}
        if self.engine_mode == "subprocess":
            for d in _site_dirs():
                if d not in worker_mod.EXTRA_PATHS:
                    worker_mod.EXTRA_PATHS.append(d)

    # ------------------------------------------------------------------ small helpers
    def _jget(self, key: str) -> dict | None:
        raw = self.kv.get(key)
        if not raw:
            return None
        try:
            v = json.loads(raw)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None

    def rate_ok(self, what: str, who: str, per_minute: int) -> bool:
        tag = hashlib.sha256(f"{what}|{who}".encode()).hexdigest()[:16]
        try:
            return self.kv.incr(f"{P}rl:{tag}:{int(self.clock() // 60)}", 70) <= per_minute
        except KVError:
            return True                    # a store hiccup never locks the owner out

    # ================================================================== devices
    def _devices(self) -> dict[str, dict]:
        out = {}
        for did, raw in self.kv.hgetall(f"{P}devices").items():
            try:
                out[did] = json.loads(raw)
            except ValueError:
                continue
        return out

    def list_devices(self) -> list[dict]:
        devs = self._devices()
        return sorted(({"device_id": d, **v} for d, v in devs.items()), key=lambda x: x.get("created", 0))

    def register_device(self, name: str) -> tuple[str, str]:
        if not valid_device_name(name):
            raise CloudError("bad_request")
        if len(self._devices()) >= self.s.devices_max:
            raise CloudError("too_many_devices", 409)
        did = secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        self.kv.set(f"{P}tok:{self.keys.token_hash(token)}", did)
        self.kv.hset(f"{P}devices", {did: json.dumps({"name": name, "created": int(self.clock()),
                                                      "th": self.keys.token_hash(token)})})
        log.info("device registered %s", did)
        return did, token

    def authenticate(self, authorization: str | None) -> str | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:].strip()
        if not 20 <= len(token) <= 128:
            return None
        try:
            did = self.kv.get(f"{P}tok:{self.keys.token_hash(token)}")
        except KVError:
            raise CloudError("unavailable", 503) from None
        if not did or not is_device_id(did):
            return None
        if did not in self._dev_seen or self.clock() - self._dev_seen[did] > 30:
            if self.kv.hget(f"{P}devices", did) is None:      # revoked: the token key may outlive the record briefly
                return None
            self._dev_seen[did] = self.clock()
        return did

    def device_name(self, did: str) -> str:
        v = self._devices().get(did)
        return (v or {}).get("name", "")

    def revoke(self, did: str) -> bool:
        v = self._devices().get(did)
        if v is None:
            return False
        self.kv.hdel(f"{P}devices", did)
        if v.get("th"):
            self.kv.delete(f"{P}tok:{v['th']}")
        self._dev_seen.pop(did, None)
        log.info("device revoked %s", did)
        return True

    # ================================================================== pairing (OSP1 with the platform certificate)
    def open_pairing(self) -> dict:
        code = new_code()
        exp = int(self.clock() + PAIR_WINDOW_S)
        self.kv.set(f"{P}pairwin", self.keys.seal({"code": code, "exp": exp}, "pairwin"), ex=PAIR_WINDOW_S)
        self.kv.delete(f"{P}pairfail")
        return {"code": display_code(code), "expires": exp}

    def close_pairing(self) -> None:
        self.kv.delete(f"{P}pairwin", f"{P}pairfail")

    def _window(self) -> dict | None:
        raw = self.kv.get(f"{P}pairwin")
        w = self.keys.unseal(raw, "pairwin") if raw else None
        if not w or w.get("exp", 0) <= self.clock():
            return None
        return w

    def pairing_open(self) -> bool:
        try:
            return self._window() is not None
        except KVError:
            return False

    def pair_start(self, ip: str) -> dict:
        if not self.rate_ok("pairstart", ip, 30):
            raise CloudError("rate_limited", 429, 60)
        w = self._window()
        if w is None:
            raise CloudError("pairing_closed", 409)
        pair_id, salt = secrets.token_hex(16), secrets.token_hex(16)
        ttl = max(1, int(w["exp"] - self.clock()))
        self.kv.set(f"{P}pairid:{pair_id}", salt, ex=ttl)
        return {"pair_id": pair_id, "salt": salt, "spki_pin": CLOUD_PIN, "iterations": ITERATIONS, "tls": "ca"}

    def pair_claim(self, ip: str, pair_id: str, device_name: str, proof: str) -> dict:
        if not self.rate_ok("pairclaim", "all", 10) or not self.rate_ok("pairclaim", ip, 5):
            raise CloudError("rate_limited", 429, 60)
        w = self._window()
        salt = self.kv.get(f"{P}pairid:{pair_id}") if is_preview_id(pair_id) else None
        if w is None or not salt or not valid_device_name(device_name) or len(proof) != 64:
            raise CloudError("pairing_failed", 403)
        k = derive_key(w["code"], pair_id, salt, CLOUD_PIN, ITERATIONS)
        if not hmac.compare_digest(client_proof(k, pair_id, device_name), proof.lower()):
            if self.kv.incr(f"{P}pairfail", PAIR_WINDOW_S) >= MAX_ATTEMPTS:
                self.close_pairing()                 # burned after 5 wrong proofs
            raise CloudError("pairing_failed", 403)
        self.close_pairing()                         # single use
        self.kv.delete(f"{P}pairid:{pair_id}")
        did, token = self.register_device(device_name)
        return {"device_id": did, "token": token, "server_proof": server_proof(k, pair_id, token)}

    # ================================================================== source handles (per process)
    def proxy(self) -> RangeProxy:
        with self._lock:
            if self._proxy is None:
                self._proxy = RangeProxy().start()
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap, name="openseek-cloud-reaper", daemon=True)
                self._reaper.start()
            return self._proxy

    def _reap(self) -> None:
        while True:
            time.sleep(5)
            now = time.monotonic()
            with self._lock:
                idle = [h for h in self._handles.values() if now - h.last_used > self.s.idle_close_s]
            for h in idle:
                if h.lock.acquire(blocking=False):
                    try:
                        if time.monotonic() - h.last_used > self.s.idle_close_s:
                            self._drop(h)
                    finally:
                        h.lock.release()

    def _drop(self, h: Handle) -> None:
        h.dead = True
        with self._lock:
            if self._handles.get(h.fp) is h:
                del self._handles[h.fp]
        try:
            if h.engine is not None:
                h.engine.close()
        except Exception:  # noqa: BLE001
            pass
        if self._proxy is not None:
            self._proxy.unregister(h.cap)
        h.upstream.close()

    def close(self) -> None:
        with self._lock:
            hs = list(self._handles.values())
        for h in hs:
            self._drop(h)
        if self._proxy is not None:
            self._proxy.stop()

    def _preflight(self, url: str, headers: dict) -> tuple[Upstream, object]:
        up = Upstream(self.policy, url, headers, Budget(self.s.job_bytes))
        try:
            pre = up.preflight()
        except Exception:
            up.close()
            raise
        return up, pre

    def _attach(self, up: Upstream, pre) -> Handle:
        proxy = self.proxy()
        psrc = ProxySource(up, pre.size, [(0, pre.head), (pre.tail_offset, pre.tail)])
        cap = proxy.register(psrc)
        h = Handle(pre.fp, up, psrc, cap, pre.size, pre.fmt)
        with self._lock:
            old = self._handles.get(pre.fp)
            self._handles[pre.fp] = h
        if old is not None and old is not h:
            threading.Thread(target=self._drop_when_free, args=(old,), daemon=True).start()
        return h

    def _drop_when_free(self, h: Handle) -> None:
        with h.lock:
            self._drop(h)

    def _new_engine(self, h: Handle):
        url = self.proxy().url(h.cap)
        args = (url, h.size, h.fmt, self.s.open_timeout_s, self.s.frame_timeout_s)
        if self.engine_mode == "subprocess":
            eng = WorkerEngine(*args, 8192, 12 * 3600, self.s.tolerance_ms)
            try:
                return eng, eng.open()
            except WorkerError as e:
                if not e.transient:
                    raise
                if self.s.on_vercel or os.environ.get("OPENSEEK_CLOUD_ENGINE_FALLBACK", "1") == "1":
                    log.warning("worker subprocess unavailable (%s); extracting in-process", e.code)
                    self.engine_mode = "inprocess"
                else:
                    raise
        eng = InProcessEngine(*args, tolerance_ms=self.s.tolerance_ms)
        return eng, eng.open()

    def _handle_for(self, rec: dict) -> Handle:
        """The live handle for a preview's source, re-opened from the sealed link when this instance has none."""
        fp = rec["fp"]
        with self._lock:
            h = self._handles.get(fp)
        if h is not None and not h.dead:
            return h
        up, pre = self._preflight(rec["url"], rec.get("headers") or {})
        if pre.fp != fp:
            up.close()
            raise CloudError("source_expired", 200)
        return self._attach(up, pre)

    # ================================================================== previews
    def _pv_key(self, pid: str) -> str:
        return f"{P}pv:{pid}"

    def _load_pv(self, pid: str) -> dict | None:
        if not is_preview_id(pid):
            return None
        raw = self.kv.get(self._pv_key(pid))
        return self.keys.unseal(raw, "pv:" + pid) if raw else None

    def _save_pv(self, pid: str, rec: dict) -> None:
        self.kv.set(self._pv_key(pid), self.keys.seal(rec, "pv:" + pid), ex=PREVIEW_TTL_S)

    def _meta(self, fp: str) -> dict | None:
        return self._jget(f"{P}fp:{fp}")

    def _fail(self, fp: str) -> str | None:
        return self.kv.get(f"{P}fail:{fp}")

    def _set_fail(self, fp: str, code: str) -> None:
        self.kv.set(f"{P}fail:{fp}", code, ex=FAIL_TTL_S)

    def tiles(self, fp: str) -> dict[int, int]:
        out = {}
        for k, v in self.kv.hgetall(f"{P}tiles:{fp}:{PROFILE}").items():
            if k.isdigit() and v.lstrip("-").isdigit():
                out[int(k)] = int(v)
        return out

    def n_tiles(self, duration_ms: int) -> int:
        return max(1, (int(duration_ms) - 1) // self.s.tile_interval_ms + 1)

    def create(self, device_id: str, url: str, headers: dict, layout: str, at_ms: int | None,
               playhead_ms: int | None, base: str) -> dict:
        try:
            self.policy.check_syntax(url)
        except Denied:
            raise CloudError("source_forbidden") from None
        if not self.rate_ok("create", device_id, 30):
            raise CloudError("busy", 429, 30)
        try:
            up, pre = self._preflight(url, headers)
        except UpstreamError as e:
            if e.code == "source_forbidden":
                raise CloudError("source_forbidden") from None
            safe = e.reason if e.reason.replace("_", "").isalnum() else "other"
            log.warning("preflight failed code=%s reason=%s", e.code, safe)
            return self._failed_body(device_id, url, e.code if e.code != "internal" else "internal")
        err = None
        if pre.fmt is None:
            err = "not_video"
        elif pre.fmt == "matroska" and not pre.has_index:
            err = "no_index"
        if err:
            up.close()
            return self._failed_body(device_id, url, err)
        fp = pre.fp
        pid = self.keys.preview_id(device_id, fp)
        rec = {"device": device_id, "fp": fp, "url": url, "headers": headers, "size": pre.size, "fmt": pre.fmt}
        self._save_pv(pid, rec)
        self._set_priority(pid, at_ms, playhead_ms)
        meta = self._meta(fp)
        self.kv.delete(f"{P}fail:{fp}")                  # a new request for the content retries a past failure
        if meta and not self._targets(fp, meta.get("duration_ms"), self.tiles(fp), pid, limit=1):
            up.close()                                    # nothing to make right now: keep no connection open
        else:
            h = self._attach(up, pre)
            self._work(pid, rec, self.s.poll_work_s, handle=h)
        log.info("preview %s source %s", pid[:8], pre.final_host_tag)
        return self.status(device_id, pid, base, work_s=0.0)

    def _failed_body(self, device_id: str, url: str, code: str) -> dict:
        pid = self.keys.preview_id(device_id, "err:" + hashlib.sha256(url.encode()).hexdigest())
        self.kv.set(f"{P}pverr:{pid}", json.dumps({"device": device_id, "code": code}), ex=FAIL_TTL_S)
        return self._failed_dict(pid, code)

    def _failed_dict(self, pid: str, code: str) -> dict:
        return {"preview_id": pid, "state": "failed", "error": code, "vtt_url": None, "vtt_version": 0,
                "source_duration_ms": None, "tile_interval_ms": self.s.tile_interval_ms,
                "tolerance_ms": self.s.tolerance_ms, "progress": {"tiles_done": 0, "tiles_total": None},
                "refresh_after_ms": 10_000}

    def _set_priority(self, pid: str, at_ms: int | None, playhead_ms: int | None) -> None:
        cur = self._jget(f"{P}pri:{pid}") or {}
        if playhead_ms is not None:
            cur["ph"] = int(playhead_ms)
        cur["at"] = None if at_ms is None else int(at_ms)
        cur["ts"] = self.clock()
        self.kv.set(f"{P}pri:{pid}", json.dumps(cur), ex=PREVIEW_TTL_S)

    def _owned(self, device_id: str, pid: str) -> dict:
        rec = self._load_pv(pid)
        if rec is None or rec.get("device") != device_id:
            if is_preview_id(pid):
                e = self._jget(f"{P}pverr:{pid}")
                if e and e.get("device") == device_id:
                    raise CloudError(f"failed:{e.get('code')}", 200)
            raise CloudError("not_found", 404)
        return rec

    def status(self, device_id: str, pid: str, base: str, work_s: float | None = None,
               wait_version: int | None = None, wait_ms: int = 0) -> dict:
        try:
            rec = self._owned(device_id, pid)
        except CloudError as e:
            if e.code.startswith("failed:"):
                return self._failed_dict(pid, e.code[7:])
            raise
        started = time.monotonic()
        budget = self.s.poll_work_s if work_s is None else work_s
        if budget > 0:
            self._work(pid, rec, budget)
        body = self._body(pid, rec, base)
        deadline = started + min(max(wait_ms, 0), 2000) / 1000.0
        while (wait_version is not None and body["vtt_version"] <= wait_version and body["state"] != "failed"
               and time.monotonic() < deadline):
            time.sleep(0.3)
            body = self._body(pid, rec, base)
        self._save_pv(pid, rec)                           # refresh the 6 h lifetime while the client is active
        return body

    def priority(self, device_id: str, pid: str, at_ms: int | None, playhead_ms: int | None, base: str) -> dict:
        try:
            rec = self._owned(device_id, pid)
        except CloudError as e:
            if e.code.startswith("failed:"):
                return {"vtt_version": 0, "state": "failed"}
            raise
        self._set_priority(pid, at_ms, playhead_ms)
        if at_ms is not None or playhead_ms is not None:
            self._work(pid, rec, self.s.priority_work_s)
        b = self._body(pid, rec, base)
        return {"vtt_version": b["vtt_version"], "state": b["state"]}

    def delete(self, device_id: str, pid: str) -> None:
        try:
            self._owned(device_id, pid)
        except CloudError as e:
            if e.code.startswith("failed:"):
                self.kv.delete(f"{P}pverr:{pid}")
                return
            raise
        self.kv.delete(self._pv_key(pid), f"{P}pri:{pid}")

    def _body(self, pid: str, rec: dict, base: str) -> dict:
        fp = rec["fp"]
        meta = self._meta(fp)
        fail = self._fail(fp)
        done = self.tiles(fp)
        dur = meta.get("duration_ms") if meta else None
        total = self.n_tiles(dur) if dur else None
        if fail and not done:
            state = "failed"
        elif total and len(done) >= total:
            state = "complete"
        elif done:
            state = "ready"
        else:
            state = "preparing"
        exp = self.keys.caps.current_exp()
        cap = self.keys.caps.cap(rec["device"], pid, exp)
        refresh = {"preparing": 1000, "ready": 2000}.get(state, 10_000)
        if state == "ready" and self._targets(fp, dur, done, pid, limit=1) == []:
            refresh = 10_000                       # nothing left this pregen mode would make on its own
        return {"preview_id": pid, "state": state, "error": fail if state == "failed" else None,
                "vtt_url": f"{base}/v1/c/{cap}/{pid}/thumbs.vtt?exp={exp}&layout=tiles",
                "vtt_version": len(done), "source_duration_ms": dur,
                "tile_interval_ms": self.s.tile_interval_ms, "tolerance_ms": self.s.tolerance_ms,
                "progress": {"tiles_done": len(done), "tiles_total": total}, "refresh_after_ms": refresh}

    # ================================================================== scheduling
    def _targets(self, fp: str, dur: int | None, done: dict[int, int], pid: str, limit: int = 64) -> list[int]:
        if not dur:
            return []
        n = self.n_tiles(dur)
        bad = {int(k) for k in self.kv.hgetall(f"{P}bad:{fp}") if k.isdigit()}
        skip = set(done) | bad
        pri = self._jget(f"{P}pri:{pid}") or {}
        ph = max(0, min(n - 1, int(pri.get("ph") or 0) // self.s.tile_interval_ms))
        out: list[int] = []

        def add(k: int) -> bool:
            if 0 <= k < n and k not in skip and k not in out:
                out.append(k)
            return len(out) >= limit

        at = pri.get("at")
        if at is not None and self.clock() - float(pri.get("ts") or 0) < 20:
            k0 = max(0, min(n - 1, int(round(int(at) / self.s.tile_interval_ms))))
            for d in range(P0_SPAN + 1):
                if add(k0 + d) or (d and add(k0 - d)):
                    return out
        if self.s.pregen == "off" and at is None:
            return out
        span = 18 if self.s.pregen == "full" else 6
        for d in range(span + 1):
            if add(ph + d) or (d and add(ph - d)):
                return out
        if self.s.pregen == "off":
            return out
        steps = (6,) if self.s.pregen == "overview" else (6, 3, 1)
        for step in steps:
            for k in sorted(range(0, n, step), key=lambda k: abs(k - ph)):
                if add(k):
                    return out
        return out

    def _work(self, pid: str, rec: dict, budget_s: float, handle: Handle | None = None) -> int:
        fp = rec["fp"]
        if budget_s <= 0 or self._fail(fp):
            return 0
        lock_key, me = f"{P}work:{fp}", secrets.token_hex(8)
        hold = int(budget_s + 2 * self.s.frame_timeout_s + self.s.open_timeout_s + 5)
        try:
            if not self.kv.set(lock_key, me, ex=hold, nx=True):
                return 0                                  # another request is extracting this source right now
        except KVError:
            return 0
        made = 0
        try:
            deadline = time.monotonic() + budget_s
            h = handle or self._handle_for(rec)
            with h.lock:
                h.last_used = time.monotonic()
                if h.engine is None or not h.engine.is_open():
                    h.engine, h.info = self._new_engine(h)
                    meta = {"duration_ms": h.info.duration_ms, "width": h.info.width, "height": h.info.height,
                            "codec": h.info.codec, "hdr": h.info.hdr}
                    self.kv.set(f"{P}fp:{fp}", json.dumps(meta), ex=self.s.tile_ttl_days * 86400)
                dur = h.info.duration_ms
                tol = self.s.tolerance_ms
                while time.monotonic() < deadline:
                    done = self.tiles(fp)
                    nxt = self._targets(fp, dur, done, pid, limit=1)
                    if not nxt:
                        break
                    k = nxt[0]
                    t_ms = min(k * self.s.tile_interval_ms, max(0, dur - 1))
                    h.psrc.last_error = None
                    try:
                        res = h.engine.grab(t_ms, accurate=False)
                        if abs(res.pts_ms - t_ms) > tol:
                            res = h.engine.grab(t_ms, accurate=True)
                    except WorkerError as e:
                        up = h.psrc.last_error.code if h.psrc.last_error is not None else None
                        if up in FATAL_UPSTREAM:
                            self._set_fail(fp, up)
                            self._drop(h)
                            break
                        if e.code in ("not_video", "unsupported_codec"):
                            if not done:
                                self._set_fail(fp, e.code)
                                break
                            self.kv.hset(f"{P}bad:{fp}", {str(k): "1"}, ex=FAIL_TTL_S)
                            continue
                        if e.transient or up == "source_unreachable":
                            h.engine.close()
                            h.engine = None
                            break                         # next request starts a fresh worker
                        self.kv.hset(f"{P}bad:{fp}", {str(k): "1"}, ex=FAIL_TTL_S)
                        continue
                    if not jpeg_ok(res.jpeg):
                        self.kv.hset(f"{P}bad:{fp}", {str(k): "1"}, ex=FAIL_TTL_S)
                        continue
                    name = self.keys.store_name(f"{fp}/{PROFILE}", k)
                    try:
                        self.store.put(name, res.jpeg)
                    except StorageError as e:
                        log.warning("storage put failed: %s", e)
                        break
                    self._remember(name, res.jpeg)
                    self.kv.hset(f"{P}tiles:{fp}:{PROFILE}", {str(k): str(res.pts_ms)},
                                 ex=self.s.tile_ttl_days * 86400)
                    made += 1
                h.last_used = time.monotonic()
        except CloudError as e:
            self._set_fail(fp, e.code)
        except UpstreamError as e:
            self._set_fail(fp, e.code if e.code in FATAL_UPSTREAM or e.code in (
                "source_unreachable", "range_unsupported", "not_video") else "source_unreachable")
        except WorkerError as e:
            if not e.transient:
                self._set_fail(fp, e.code if e.code in ("not_video", "unsupported_codec") else "internal")
        finally:
            try:
                if self.kv.get(lock_key) == me:
                    self.kv.delete(lock_key)
            except KVError:
                pass
        if made:
            log.info("source %s: %d tile(s)", fp[:8], made)
        return made

    # ================================================================== data plane
    def _remember(self, name: str, data: bytes) -> None:
        with self._lock:
            self._recent[name] = data
            self._recent.move_to_end(name)
            while len(self._recent) > 256:
                self._recent.popitem(last=False)

    def _cap_rec(self, cap: str, pid: str, exp: int) -> dict | None:
        rec = self._load_pv(pid)
        if rec is None or not self.keys.caps.verify(cap, rec["device"], pid, exp):
            return None
        return rec

    def vtt(self, cap: str, pid: str, exp: int, base: str) -> str | None:
        rec = self._cap_rec(cap, pid, exp)
        if rec is None:
            return None
        meta = self._meta(rec["fp"]) or {}
        dur = meta.get("duration_ms")
        done = self.tiles(rec["fp"])
        return render(base, cap, pid, exp, "tiles", done, dur, self.n_tiles(dur) if dur else None,
                      self.s.tolerance_ms)

    def tile(self, cap: str, pid: str, exp: int, k: int) -> bytes | None:
        rec = self._cap_rec(cap, pid, exp)
        if rec is None:
            return None
        name = self.keys.store_name(f"{rec['fp']}/{PROFILE}", k)
        with self._lock:
            hit = self._recent.get(name)
        if hit is not None:
            return hit
        if self.kv.hget(f"{P}tiles:{rec['fp']}:{PROFILE}", str(k)) is None:
            return b""
        try:
            data = self.store.get(name)
        except StorageError:
            return b""
        if data:
            self._remember(name, data)
        return data or b""

    # ================================================================== owner page
    def overview(self) -> dict:
        try:
            kv_ok = self.kv.ping()
        except KVError:
            kv_ok = False
        return {"version": __version__, "kv": "upstash" if self.s.kv_configured else "memory", "kv_ok": kv_ok,
                "kv_persistent": bool(getattr(self.kv, "persistent", True)), "storage": self.store.kind,
                "pregen": self.s.pregen, "engine": self.engine_mode, "pairing_open": self.pairing_open(),
                "devices": self.list_devices() if kv_ok else []}

    def check_storage(self) -> bool:
        return self.store.check()

