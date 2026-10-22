"""Key-value store: Upstash Redis over its REST API, or an in-memory store for a single container.

Only a handful of Redis commands are used, so the client is a few lines of urllib (no extra dependency).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request


class KVError(Exception):
    pass


class KV:
    """Interface. Values are str. `ex` = expiry in seconds."""

    persistent = True

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool: ...
    def delete(self, *keys: str) -> None: ...
    def incr(self, key: str, ex: int) -> int: ...
    def hset(self, key: str, mapping: dict[str, str], ex: int | None = None) -> None: ...
    def hget(self, key: str, field: str) -> str | None: ...
    def hgetall(self, key: str) -> dict[str, str]: ...
    def hdel(self, key: str, field: str) -> None: ...
    def hlen(self, key: str) -> int: ...
    def ping(self) -> bool: ...


class UpstashKV(KV):
    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, body) -> object:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise KVError(type(e).__name__) from None
        return out

    def cmd(self, *args) -> object:
        out = self._post("", [str(a) for a in args])
        if not isinstance(out, dict) or "error" in out:
            raise KVError("command")
        return out.get("result")

    def pipeline(self, cmds: list[list]) -> list:
        out = self._post("/pipeline", [[str(a) for a in c] for c in cmds])
        if not isinstance(out, list):
            raise KVError("pipeline")
        res = []
        for item in out:
            if not isinstance(item, dict) or "error" in item:
                raise KVError("command")
            res.append(item.get("result"))
        return res

    def get(self, key):
        v = self.cmd("GET", key)
        return v if isinstance(v, str) else None

    def set(self, key, value, ex=None, nx=False):
        args = ["SET", key, value]
        if ex:
            args += ["EX", int(ex)]
        if nx:
            args.append("NX")
        return self.cmd(*args) == "OK"

    def delete(self, *keys):
        if keys:
            self.cmd("DEL", *keys)

    def incr(self, key, ex):
        v, _ = self.pipeline([["INCR", key], ["EXPIRE", key, int(ex)]])     # callers put the time window in the key
        return int(v)

    def hset(self, key, mapping, ex=None):
        if not mapping:
            return
        flat = []
        for k, v in mapping.items():
            flat += [k, v]
        cmds = [["HSET", key, *flat]]
        if ex:
            cmds.append(["EXPIRE", key, int(ex)])
        self.pipeline(cmds)

    def hget(self, key, field):
        v = self.cmd("HGET", key, field)
        return v if isinstance(v, str) else None

    def hgetall(self, key):
        v = self.cmd("HGETALL", key) or []
        if isinstance(v, dict):
            return {str(a): str(b) for a, b in v.items()}
        return {str(v[i]): str(v[i + 1]) for i in range(0, len(v) - 1, 2)}

    def hdel(self, key, field):
        self.cmd("HDEL", key, field)

    def hlen(self, key):
        return int(self.cmd("HLEN", key) or 0)

    def ping(self):
        try:
            return self.cmd("PING") == "PONG"
        except KVError:
            return False


class MemoryKV(KV):
    """Single-process store. Everything is lost on restart (devices must pair again): fine for a trial, not for
    Vercel, where every request may run in a different instance."""

    persistent = False

    def __init__(self, clock=time.time):
        self._d: dict[str, tuple[object, float | None]] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def _live(self, key):
        item = self._d.get(key)
        if item is None:
            return None
        val, exp = item
        if exp is not None and exp <= self.clock():
            del self._d[key]
            return None
        return item

    def get(self, key):
        with self._lock:
            it = self._live(key)
            return it[0] if it and isinstance(it[0], str) else None

    def set(self, key, value, ex=None, nx=False):
        with self._lock:
            if nx and self._live(key) is not None:
                return False
            self._d[key] = (str(value), self.clock() + ex if ex else None)
            return True

    def delete(self, *keys):
        with self._lock:
            for k in keys:
                self._d.pop(k, None)

    def incr(self, key, ex):
        with self._lock:
            it = self._live(key)
            if it is None:
                self._d[key] = ("1", self.clock() + ex)
                return 1
            v = int(it[0]) + 1
            self._d[key] = (str(v), it[1])
            return v

    def hset(self, key, mapping, ex=None):
        with self._lock:
            it = self._live(key)
            h = dict(it[0]) if it and isinstance(it[0], dict) else {}
            h.update({str(k): str(v) for k, v in mapping.items()})
            exp = self.clock() + ex if ex else (it[1] if it else None)
            self._d[key] = (h, exp)

    def hget(self, key, field):
        with self._lock:
            it = self._live(key)
            return it[0].get(field) if it and isinstance(it[0], dict) else None

    def hgetall(self, key):
        with self._lock:
            it = self._live(key)
            return dict(it[0]) if it and isinstance(it[0], dict) else {}

    def hdel(self, key, field):
        with self._lock:
            it = self._live(key)
            if it and isinstance(it[0], dict):
                it[0].pop(field, None)

    def hlen(self, key):
        return len(self.hgetall(key))

    def ping(self):
        return True


def make_kv(settings) -> KV:
    if settings.kv_configured:
        return UpstashKV(settings.kv_url, settings.kv_token)
    return MemoryKV()
