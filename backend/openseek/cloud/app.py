"""The cloud HTTP app: API contract v1 for devices, plus the owner page at /configure.

Differences from the self-hosted listener (see docs/CLOUD.md):
* TLS is the platform's normal certificate. Pairing (OSP1) still works; `pair/start` answers
  `"spki_pin": "ca", "tls": "ca"`, so the client verifies the certificate the usual way instead of pinning.
* Layout is always `tiles` (sheets would need every tile of a block made first).
* The owner page replaces the loopback admin listener; it is protected by OPENSEEK_ADMIN_PIN.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import API_VERSION, __version__
from ..app import PreviewIn, PriorityIn
from ..ids import is_cap, is_device_id, is_preview_id, parse_index
from ..logutil import setup_logging
from ..netpolicy import filter_forwarded_headers
from .crypto import pin_matches
from .page import OWNER_PAGE
from .runtime import Cloud, CloudError
from .settings import CloudConfigError, CloudSettings

log = logging.getLogger("openseek.cloud.app")

MAX_BODY = 16 * 1024
SESSION_COOKIE = "openseek_owner"
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}(?::\d{1,5})?$")
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pair_id: StrictStr = Field(min_length=32, max_length=32)
    device_name: StrictStr = Field(min_length=1, max_length=64)
    client_proof: StrictStr = Field(min_length=64, max_length=64)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pin: StrictStr = Field(min_length=1, max_length=256)


class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: StrictStr = Field(min_length=1, max_length=64)


def err(status: int, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def create_cloud_app(cloud: Cloud | None = None, settings: CloudSettings | None = None) -> FastAPI:
    setup_logging()
    config_error = None
    if cloud is None:
        try:
            settings = settings or CloudSettings.from_env()
            cloud = Cloud(settings)
        except CloudConfigError as e:
            config_error = str(e)
            log.error("configuration error: %s", e)
    s = cloud.s if cloud else None

    app = FastAPI(title="OpenSeek Cloud", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    # ------------------------------------------------------------------ helpers
    def base_url(request: Request) -> str | None:
        if s and s.public_url:
            return s.public_url
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if not _HOST_RE.fullmatch(host):
            return None
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
        if proto not in ("http", "https"):
            return None
        if proto == "http" and host.split(":")[0] not in ("localhost", "127.0.0.1"):
            proto = "https"          # behind a TLS-terminating platform the app itself only sees http
        return f"{proto}://{host}"

    def client_ip(request: Request) -> str:
        ip = request.headers.get("x-real-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0]
        ip = ip.strip()
        if not ip and request.client:
            ip = request.client.host
        return ip[:64] or "?"

    def device(request: Request) -> str | None:
        return getattr(request.state, "device_id", None)

    def cloud_error(e: CloudError) -> JSONResponse:
        h = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        return err(e.status if e.status >= 400 else 400, e.code, h)

    def owner_ok(request: Request) -> bool:
        return bool(cloud) and cloud.keys.session_ok(request.cookies.get(SESSION_COOKIE))

    def csrf_ok(request: Request) -> bool:
        sess = request.cookies.get(SESSION_COOKIE) or ""
        return bool(sess) and request.headers.get("x-openseek-csrf") == cloud.keys.csrf(sess)

    # ------------------------------------------------------------------ guard middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if config_error and path not in ("/", "/v1/health", "/configure"):
            return err(503, "unavailable")
        clen = request.headers.get("content-length")
        if clen is not None and (not clen.isdigit() or int(clen) > MAX_BODY):
            return err(413, "too_large")
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > MAX_BODY:
                return err(413, "too_large")
        request.state.device_id = None
        needs_auth = path.startswith("/v1/") and not path.startswith("/v1/c/") and path not in (
            "/v1/health", "/v1/pair/start", "/v1/pair/claim")
        if needs_auth:
            try:
                did = await asyncio.to_thread(cloud.authenticate, request.headers.get("authorization"))
            except CloudError as e:
                return cloud_error(e)
            if did is None:
                return err(401, "unauthorized")
            request.state.device_id = did
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            if k == "cache-control" and k in resp.headers:
                continue
            resp.headers.setdefault(k, v)
        return resp

    @app.exception_handler(RequestValidationError)
    async def _bad(request, exc):
        return err(400, "bad_request")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request, exc):
        return err(exc.status_code, {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error"))

    @app.exception_handler(Exception)
    async def _crash(request, exc):
        log.error("unhandled %s", type(exc).__name__)
        return err(500, "internal")

    # ------------------------------------------------------------------ public
    @app.get("/")
    async def root():
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>OpenSeek Cloud</title>'
                            '<p>OpenSeek Cloud is running. Owner page: <a href="/configure">/configure</a></p>')

    @app.get("/v1/health")
    async def health():
        if config_error:
            return JSONResponse({"service": "openseek", "api": API_VERSION, "mode": "cloud", "ok": False,
                                 "error": "config"}, status_code=503)
        return {"service": "openseek", "api": API_VERSION, "version": __version__, "mode": "cloud",
                "ffmpeg": True, "pairing_open": await asyncio.to_thread(cloud.pairing_open)}

    @app.post("/v1/pair/start")
    async def pair_start(request: Request):
        try:
            return await asyncio.to_thread(cloud.pair_start, client_ip(request))
        except CloudError as e:
            return cloud_error(e)

    @app.post("/v1/pair/claim")
    async def pair_claim(request: Request, body: ClaimIn):
        try:
            return await asyncio.to_thread(cloud.pair_claim, client_ip(request), body.pair_id, body.device_name,
                                           body.client_proof)
        except CloudError as e:
            return cloud_error(e)

    @app.get("/v1/pair/status")
    async def pair_status(request: Request):
        d = device(request)
        return {"device_id": d, "device_name": await asyncio.to_thread(cloud.device_name, d)}

    @app.delete("/v1/pair")
    async def unpair(request: Request):
        await asyncio.to_thread(cloud.revoke, device(request))
        return Response(status_code=204)

    # ------------------------------------------------------------------ previews
    @app.post("/v1/previews", status_code=202)
    async def create_preview(request: Request, body: PreviewIn):
        if request.url.query:
            return err(400, "bad_request")              # URLs never travel in query strings
        base = base_url(request)
        if base is None:
            return err(400, "bad_request")
        headers = filter_forwarded_headers(body.source.headers)
        try:
            out = await asyncio.to_thread(cloud.create, device(request), body.source.url, headers, body.layout,
                                          body.at_ms, body.playhead_ms, base)
        except CloudError as e:
            return cloud_error(e)
        return JSONResponse(out, status_code=202)

    @app.get("/v1/previews/{preview_id}")
    async def get_preview(request: Request, preview_id: str, wait_version: int | None = None, wait_ms: int = 0):
        if not is_preview_id(preview_id):
            return err(404, "not_found")
        base = base_url(request)
        if base is None:
            return err(400, "bad_request")
        try:
            return await asyncio.to_thread(cloud.status, device(request), preview_id, base, None, wait_version,
                                           max(0, min(int(wait_ms), 2000)))
        except CloudError as e:
            return cloud_error(e)

    @app.post("/v1/previews/{preview_id}/priority")
    async def priority(request: Request, preview_id: str, body: PriorityIn):
        if not is_preview_id(preview_id):
            return err(404, "not_found")
        base = base_url(request)
        try:
            return await asyncio.to_thread(cloud.priority, device(request), preview_id, body.at_ms,
                                           body.playhead_ms, base or "")
        except CloudError as e:
            return cloud_error(e)

    @app.delete("/v1/previews/{preview_id}")
    async def delete_preview(request: Request, preview_id: str):
        if not is_preview_id(preview_id):
            return err(404, "not_found")
        try:
            await asyncio.to_thread(cloud.delete, device(request), preview_id)
        except CloudError as e:
            return cloud_error(e)
        return Response(status_code=204)

    # ------------------------------------------------------------------ data plane (capability URLs)
    def exp_of(request: Request) -> int | None:
        raw = request.query_params.get("exp", "")
        return int(raw) if raw.isdigit() and len(raw) <= 12 else None

    @app.get("/v1/c/{cap}/{preview_id}/thumbs.vtt")
    async def vtt(request: Request, cap: str, preview_id: str):
        exp = exp_of(request)
        base = base_url(request)
        if not is_cap(cap) or not is_preview_id(preview_id) or exp is None or base is None:
            return err(403, "forbidden")
        text = await asyncio.to_thread(cloud.vtt, cap, preview_id, exp, base)
        if text is None:
            return err(403, "forbidden")
        return Response(text, media_type="text/vtt", headers={"Cache-Control": "no-store"})

    @app.get("/v1/c/{cap}/{preview_id}/tiles/{k}.jpg")
    async def tile(request: Request, cap: str, preview_id: str, k: str):
        exp = exp_of(request)
        idx = parse_index(k)
        if not is_cap(cap) or not is_preview_id(preview_id) or exp is None or idx is None:
            return err(403, "forbidden")
        data = await asyncio.to_thread(cloud.tile, cap, preview_id, exp, idx)
        if data is None:
            return err(403, "forbidden")
        if not data:
            return err(404, "not_found")
        # Immutable: the platform CDN may keep it until the capability expires.
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=21600, s-maxage=21600, immutable"})

    @app.get("/v1/c/{cap}/{preview_id}/sheets/{n}.jpg")
    async def sheet(cap: str, preview_id: str, n: str):
        return err(404, "not_found")                    # the cloud VTT only ever references tiles

    # ------------------------------------------------------------------ owner page
    @app.get("/configure")
    async def configure():
        if config_error:
            html = OWNER_PAGE.replace("/*CONFIG_ERROR*/null", json.dumps(config_error).replace("<", "\\u003c"))
        else:
            html = OWNER_PAGE
        return HTMLResponse(html, headers={"Content-Security-Policy": PAGE_CSP})

    @app.post("/configure/api/login")
    async def login(request: Request, body: LoginIn):
        ip = client_ip(request)
        if not await asyncio.to_thread(cloud.rate_ok, "login", ip, 10):
            return err(429, "rate_limited", {"Retry-After": "60"})
        if not pin_matches(body.pin, s.admin_pin):
            return err(403, "wrong_pin")
        sess = cloud.keys.session_cookie()
        r = JSONResponse({"ok": True, "csrf": cloud.keys.csrf(sess)})
        r.set_cookie(SESSION_COOKIE, sess, max_age=30 * 86400, httponly=True, samesite="strict",
                     secure=(base_url(request) or "").startswith("https://"), path="/configure")
        return r

    @app.post("/configure/api/logout")
    async def logout():
        r = JSONResponse({"ok": True})
        r.delete_cookie(SESSION_COOKIE, path="/configure")
        return r

    @app.get("/configure/api/state")
    async def state(request: Request):
        if not owner_ok(request):
            return err(401, "login")
        out = await asyncio.to_thread(cloud.overview)
        out["server"] = base_url(request)
        out["csrf"] = cloud.keys.csrf(request.cookies.get(SESSION_COOKIE) or "")
        return out

    async def owner_action(request: Request, fn, *args):
        if not owner_ok(request):
            return err(401, "login")
        if not csrf_ok(request):
            return err(403, "csrf")
        try:
            return await asyncio.to_thread(fn, *args)
        except CloudError as e:
            return cloud_error(e)

    @app.post("/configure/api/pairing/open")
    async def open_pairing(request: Request):
        return await owner_action(request, cloud.open_pairing)

    @app.post("/configure/api/pairing/close")
    async def close_pairing(request: Request):
        r = await owner_action(request, cloud.close_pairing)
        return r if isinstance(r, Response) else {"ok": True}

    @app.post("/configure/api/devices")
    async def add_device(request: Request, body: DeviceIn):
        r = await owner_action(request, cloud.register_device, body.name)
        if isinstance(r, Response):
            return r
        did, token = r
        return {"device_id": did, "token": token, "server": base_url(request)}

    @app.post("/configure/api/devices/{device_id}/revoke")
    async def revoke(request: Request, device_id: str):
        if not is_device_id(device_id):
            return err(404, "not_found")
        r = await owner_action(request, cloud.revoke, device_id)
        if isinstance(r, Response):
            return r
        return {"ok": bool(r)}

    @app.post("/configure/api/storage/check")
    async def storage_check(request: Request):
        r = await owner_action(request, cloud.check_storage)
        return r if isinstance(r, Response) else {"ok": bool(r)}

    return app
