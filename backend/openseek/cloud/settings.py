"""Cloud settings, read from environment variables (Vercel project settings or `docker run -e`).

Required:
  OPENSEEK_CLOUD_SECRET   at least 32 characters; signs tokens, capability URLs and encrypts stored source links.
                          Changing it signs every device out and makes cached tiles unreachable.
  OPENSEEK_ADMIN_PIN      at least 8 characters; protects the owner page /configure.

Key-value store (required on Vercel; in-memory fallback for a single Docker container):
  KV_REST_API_URL + KV_REST_API_TOKEN, or UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN

Tile storage (OPENSEEK_STORAGE = auto | r2 | cloudinary | local; auto picks r2, then cloudinary, then local):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME
  CLOUDINARY_URL (cloudinary://key:secret@cloud) or CLOUDINARY_CLOUD_NAME + CLOUDINARY_API_KEY + CLOUDINARY_API_SECRET
  OPENSEEK_DATA_DIR       local storage directory (Docker only)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


class CloudConfigError(ValueError):
    pass


def _int(env: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise CloudConfigError(f"{name} must be a whole number") from None
    if not lo <= v <= hi:
        raise CloudConfigError(f"{name} must be between {lo} and {hi}")
    return v


def _float(env: dict, name: str, default: float, lo: float, hi: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        raise CloudConfigError(f"{name} must be a number") from None
    if not lo <= v <= hi:
        raise CloudConfigError(f"{name} must be between {lo} and {hi}")
    return v


def _bool(env: dict, name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise CloudConfigError(f"{name} must be 1 or 0")


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    endpoint: str = ""          # override (tests, other S3-compatible stores); default https://<account>.r2.cloudflarestorage.com


@dataclass(frozen=True)
class CloudinaryConfig:
    cloud_name: str
    api_key: str
    api_secret: str
    folder: str = "openseek"


@dataclass(frozen=True)
class CloudSettings:
    secret: bytes
    admin_pin: str
    kv_url: str = ""
    kv_token: str = ""
    storage: str = "local"                 # resolved: r2 | cloudinary | local
    r2: R2Config | None = None
    cloudinary: CloudinaryConfig | None = None
    data_dir: str = "data"
    public_url: str = ""                   # https://name.example.org, when the platform's Host header is not the public name
    on_vercel: bool = False
    # extraction
    pregen: str = "overview"               # off | overview (60 s grid) | full (every 10 s)
    poll_work_s: float = 1.5               # extraction time a status poll may spend before answering
    priority_work_s: float = 6.0           # ... a priority (scrub) call
    tile_interval_ms: int = 10_000
    tolerance_ms: int = 5_000
    job_bytes: int = 4 << 30
    open_timeout_s: float = 25.0
    frame_timeout_s: float = 20.0
    idle_close_s: float = 90.0
    allowed_ports: frozenset[int] = field(default_factory=lambda: frozenset({80, 443, 8080, 8443}))
    allow_http_sources: bool = True
    tile_ttl_days: int = 30
    devices_max: int = 32

    @property
    def kv_configured(self) -> bool:
        return bool(self.kv_url and self.kv_token)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CloudSettings":
        env = dict(os.environ if env is None else env)
        secret = (env.get("OPENSEEK_CLOUD_SECRET") or "").strip()
        if len(secret) < 32:
            raise CloudConfigError("OPENSEEK_CLOUD_SECRET must be set to at least 32 random characters")
        pin = (env.get("OPENSEEK_ADMIN_PIN") or "").strip()
        if len(pin) < 8:
            raise CloudConfigError("OPENSEEK_ADMIN_PIN must be set to at least 8 characters")
        on_vercel = bool(env.get("VERCEL"))

        kv_url = (env.get("KV_REST_API_URL") or env.get("UPSTASH_REDIS_REST_URL") or "").strip().rstrip("/")
        kv_token = (env.get("KV_REST_API_TOKEN") or env.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
        if kv_url and not kv_url.startswith("https://"):
            raise CloudConfigError("the Upstash REST URL must start with https://")
        if on_vercel and not (kv_url and kv_token):
            raise CloudConfigError("on Vercel an Upstash Redis store is required (KV_REST_API_URL and KV_REST_API_TOKEN)")

        r2 = None
        if env.get("R2_ACCOUNT_ID") or env.get("R2_BUCKET_NAME"):
            parts = [(env.get(k) or "").strip() for k in
                     ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")]
            if not all(parts):
                raise CloudConfigError("R2 needs R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME")
            r2 = R2Config(*parts, endpoint=(env.get("R2_ENDPOINT") or "").strip().rstrip("/"))

        cld = None
        if env.get("CLOUDINARY_URL"):
            u = urlsplit(env["CLOUDINARY_URL"].strip())
            if u.scheme != "cloudinary" or not (u.username and u.password and u.hostname):
                raise CloudConfigError("CLOUDINARY_URL must look like cloudinary://<api_key>:<api_secret>@<cloud_name>")
            cld = CloudinaryConfig(u.hostname, unquote(u.username), unquote(u.password))
        elif env.get("CLOUDINARY_CLOUD_NAME"):
            parts = [(env.get(k) or "").strip() for k in
                     ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")]
            if not all(parts):
                raise CloudConfigError("Cloudinary needs CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET")
            cld = CloudinaryConfig(*parts)
        if cld is not None and env.get("CLOUDINARY_FOLDER"):
            folder = env["CLOUDINARY_FOLDER"].strip().strip("/")
            if not folder.replace("-", "").replace("_", "").replace("/", "").isalnum():
                raise CloudConfigError("CLOUDINARY_FOLDER may only hold letters, digits, - _ /")
            cld = CloudinaryConfig(cld.cloud_name, cld.api_key, cld.api_secret, folder)

        want = (env.get("OPENSEEK_STORAGE") or "auto").strip().lower()
        if want not in ("auto", "r2", "cloudinary", "local"):
            raise CloudConfigError("OPENSEEK_STORAGE must be auto, r2, cloudinary or local")
        if want == "auto":
            want = "r2" if r2 else "cloudinary" if cld else "local"
        if want == "r2" and r2 is None:
            raise CloudConfigError("OPENSEEK_STORAGE=r2 but the R2_* variables are not set")
        if want == "cloudinary" and cld is None:
            raise CloudConfigError("OPENSEEK_STORAGE=cloudinary but the CLOUDINARY_* variables are not set")
        if want == "local" and on_vercel:
            raise CloudConfigError("on Vercel tiles need cloud storage: set the R2_* or CLOUDINARY_* variables")

        public_url = (env.get("OPENSEEK_PUBLIC_URL") or "").strip().rstrip("/")
        if public_url and not public_url.startswith("https://"):
            raise CloudConfigError("OPENSEEK_PUBLIC_URL must start with https://")

        pregen = (env.get("OPENSEEK_PREGEN") or "overview").strip().lower()
        if pregen not in ("off", "overview", "full"):
            raise CloudConfigError("OPENSEEK_PREGEN must be off, overview or full")

        ports_raw = (env.get("OPENSEEK_ALLOWED_PORTS") or "").strip()
        ports = frozenset({80, 443, 8080, 8443})
        if ports_raw:
            try:
                ports = frozenset(int(p) for p in ports_raw.split(",") if p.strip())
            except ValueError:
                raise CloudConfigError("OPENSEEK_ALLOWED_PORTS must be a comma separated list of ports") from None
            if not ports or any(not 1 <= p <= 65535 for p in ports):
                raise CloudConfigError("OPENSEEK_ALLOWED_PORTS must list ports between 1 and 65535")

        # Vercel's free plan stops a function after 300 s; keep every single request far below that.
        return cls(
            secret=secret.encode("utf-8"),
            admin_pin=pin,
            kv_url=kv_url,
            kv_token=kv_token,
            storage=want,
            r2=r2,
            cloudinary=cld,
            data_dir=(env.get("OPENSEEK_DATA_DIR") or "data").strip(),
            public_url=public_url,
            on_vercel=on_vercel,
            pregen=pregen,
            poll_work_s=_float(env, "OPENSEEK_POLL_WORK_S", 1.5, 0.0, 10.0),
            priority_work_s=_float(env, "OPENSEEK_PRIORITY_WORK_S", 6.0, 0.5, 60.0),
            tolerance_ms=int(_float(env, "OPENSEEK_TOLERANCE_S", 5.0, 0.5, 5.0) * 1000),
            job_bytes=_int(env, "OPENSEEK_JOB_BYTES", 4 << 30, 1 << 20, 1 << 40),
            open_timeout_s=_float(env, "OPENSEEK_OPEN_TIMEOUT_S", 25.0, 5.0, 120.0),
            frame_timeout_s=_float(env, "OPENSEEK_FRAME_TIMEOUT_S", 20.0, 2.0, 120.0),
            idle_close_s=_float(env, "OPENSEEK_IDLE_CLOSE_S", 90.0, 5.0, 3600.0),
            allowed_ports=ports,
            allow_http_sources=_bool(env, "OPENSEEK_ALLOW_HTTP_SOURCES", True),
            tile_ttl_days=_int(env, "OPENSEEK_RETENTION_DAYS", 30, 1, 3650),
            devices_max=_int(env, "OPENSEEK_DEVICES_MAX", 32, 1, 1000),
        )
