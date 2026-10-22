"""Tile storage: Cloudflare R2 (S3 API, SigV4), Cloudinary (signed upload API), or a local directory.

Objects are small immutable JPEGs under unguessable names (`Keys.store_name`). Which tiles exist is recorded in
the key-value store; storage is only asked for bytes it is known to hold.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..cache import atomic_write, jpeg_ok, secure_mkdir

TIMEOUT_S = 10.0


class StorageError(Exception):
    pass


class Storage:
    kind = "none"

    def put(self, name: str, data: bytes) -> None: ...
    def get(self, name: str) -> bytes | None: ...
    def check(self) -> bool: ...


def _http(req: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except OSError:
            body = b""
        return e.code, body
    except (urllib.error.URLError, OSError) as e:
        raise StorageError(type(e).__name__) from None


# --------------------------------------------------------------------------- R2 (S3 SigV4)
class R2Storage(Storage):
    kind = "r2"

    def __init__(self, cfg, clock=time.time):
        self.cfg = cfg
        self.endpoint = cfg.endpoint or f"https://{cfg.account_id}.r2.cloudflarestorage.com"
        self.host = urllib.parse.urlsplit(self.endpoint).netloc
        self.clock = clock

    def _key(self, name: str) -> str:
        return f"openseek/tiles/{name}.jpg"

    def _signed(self, method: str, key: str, body: bytes = b"", extra: dict | None = None) -> urllib.request.Request:
        now = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        path = "/" + urllib.parse.quote(self.cfg.bucket, safe="") + "/" + urllib.parse.quote(key, safe="/")
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        headers.update({k.lower(): v for k, v in (extra or {}).items()})
        signed = ";".join(sorted(headers))
        canon_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
        canon = "\n".join([method, path, "", canon_headers, signed, payload_hash])
        scope = f"{day}/auto/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canon.encode()).hexdigest()])
        k = ("AWS4" + self.cfg.secret_access_key).encode()
        for part in (day, "auto", "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.cfg.access_key_id}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={sig}")
        del headers["host"]
        return urllib.request.Request(self.endpoint + path, data=body if method == "PUT" else None,
                                      method=method, headers=headers)

    def put(self, name, data):
        st, _ = _http(self._signed("PUT", self._key(name), data,
                                   {"content-type": "image/jpeg", "cache-control": "public, max-age=31536000, immutable"}))
        if st != 200:
            raise StorageError(f"r2_put_{st}")

    def get(self, name):
        st, body = _http(self._signed("GET", self._key(name)))
        if st == 200 and jpeg_ok(body):
            return body
        if st in (403, 404):
            return None
        raise StorageError(f"r2_get_{st}")

    def check(self):
        try:
            self.put("_check", _BLACK_JPEG)
            return self.get("_check") is not None
        except StorageError:
            return False


# --------------------------------------------------------------------------- Cloudinary
class CloudinaryStorage(Storage):
    kind = "cloudinary"

    def __init__(self, cfg, clock=time.time, api_base: str = "https://api.cloudinary.com",
                 delivery_base: str = "https://res.cloudinary.com"):
        self.cfg = cfg
        self.clock = clock
        self.api_base = api_base.rstrip("/")
        self.delivery_base = delivery_base.rstrip("/")

    def _public_id(self, name: str) -> str:
        return f"{self.cfg.folder}/{name}"

    def signature(self, params: dict[str, str]) -> str:
        to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hashlib.sha1((to_sign + self.cfg.api_secret).encode("utf-8")).hexdigest()

    def put(self, name, data):
        params = {"public_id": self._public_id(name), "timestamp": str(int(self.clock())), "overwrite": "true"}
        form = dict(params, api_key=self.cfg.api_key, signature=self.signature(params),
                    file="data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"))
        req = urllib.request.Request(f"{self.api_base}/v1_1/{urllib.parse.quote(self.cfg.cloud_name)}/image/upload",
                                     data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        st, _ = _http(req)
        if st != 200:
            raise StorageError(f"cloudinary_put_{st}")

    def url(self, name: str) -> str:
        return (f"{self.delivery_base}/{urllib.parse.quote(self.cfg.cloud_name)}/image/upload/"
                f"{urllib.parse.quote(self._public_id(name))}.jpg")

    def get(self, name):
        st, body = _http(urllib.request.Request(self.url(name), method="GET"))
        if st == 200 and jpeg_ok(body):
            return body
        if st in (403, 404):
            return None
        raise StorageError(f"cloudinary_get_{st}")

    def check(self):
        try:
            self.put("_check", _BLACK_JPEG)
            return True
        except StorageError:
            return False


# --------------------------------------------------------------------------- local directory
class LocalStorage(Storage):
    kind = "local"

    def __init__(self, data_dir: str):
        self.root = Path(data_dir) / "cloud-tiles"
        secure_mkdir(self.root)

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").isalnum():
            raise StorageError("name")
        return self.root / name[:2] / f"{name}.jpg"

    def put(self, name, data):
        p = self._path(name)
        secure_mkdir(p.parent)
        atomic_write(p, data)

    def get(self, name):
        try:
            data = self._path(name).read_bytes()
        except OSError:
            return None
        return data if jpeg_ok(data) else None

    def check(self):
        return self.root.is_dir()


class MemoryStorage(Storage):
    """Tests only."""

    kind = "memory"

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, name, data):
        self.objects[name] = bytes(data)

    def get(self, name):
        return self.objects.get(name)

    def check(self):
        return True


def make_storage(settings) -> Storage:
    if settings.storage == "r2":
        return R2Storage(settings.r2)
    if settings.storage == "cloudinary":
        return CloudinaryStorage(settings.cloudinary)
    return LocalStorage(settings.data_dir)


# 16x16 black JPEG used by the storage self-check on the owner page.
def _black_jpeg() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (16, 16)).save(out, "JPEG", quality=50)
    return out.getvalue()


_BLACK_JPEG = _black_jpeg()
