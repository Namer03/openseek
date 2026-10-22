"""Keys derived from OPENSEEK_CLOUD_SECRET, sealing, ids and the owner session cookie.

Every purpose gets its own HKDF subkey, so a value made for one purpose never verifies for another.
Source links (URL + forwarded headers) are only ever stored sealed with AES-256-GCM: the key-value store never
holds a playable link in clear.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..auth import CapSigner

SESSION_TTL_S = 30 * 86400


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Keys:
    def __init__(self, secret: bytes):
        if len(secret) < 32:
            raise ValueError("secret too short")
        self._secret = secret
        self.caps = CapSigner(self.subkey("capability"))
        self._seal = AESGCM(self.subkey("seal"))
        self._pid = self.subkey("preview-id")
        self._store = self.subkey("store-name")
        self._session = self.subkey("owner-session")
        self._token = self.subkey("token-hash")

    def subkey(self, label: str) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"openseek-cloud-v1",
                    info=label.encode("ascii")).derive(self._secret)

    # ------------------------------------------------------------------ sealing
    def seal(self, obj: dict, aad: str) -> str:
        nonce = os.urandom(12)
        ct = self._seal.encrypt(nonce, json.dumps(obj, separators=(",", ":")).encode("utf-8"), aad.encode("ascii"))
        return b64u(nonce + ct)

    def unseal(self, blob: str, aad: str) -> dict | None:
        try:
            raw = b64u_dec(blob)
            return json.loads(self._seal.decrypt(raw[:12], raw[12:], aad.encode("ascii")))
        except Exception:  # noqa: BLE001 - tampered, wrong key or wrong purpose: all the same to the caller
            return None

    # ------------------------------------------------------------------ ids
    def preview_id(self, device_id: str, fp: str) -> str:
        """Same device + same content -> same id (contract v1), unlinkable across devices."""
        return hmac.new(self._pid, f"{device_id}|{fp}".encode(), hashlib.sha256).hexdigest()[:32]

    def store_name(self, fp: str, k: int) -> str:
        """Unguessable object name for a tile (Cloudinary delivery URLs are public by design)."""
        return hmac.new(self._store, f"{fp}|{int(k)}".encode(), hashlib.sha256).hexdigest()[:40]

    def token_hash(self, token: str) -> str:
        return hmac.new(self._token, token.encode("utf-8"), hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------ owner session
    def session_cookie(self, now: float | None = None) -> str:
        exp = int((now if now is not None else time.time()) + SESSION_TTL_S)
        mac = hmac.new(self._session, f"owner|{exp}".encode(), hashlib.sha256).digest()
        return f"{exp}.{b64u(mac)}"

    def session_ok(self, value: str | None, now: float | None = None) -> bool:
        if not value or "." not in value:
            return False
        exp_s, sig = value.split(".", 1)
        if not exp_s.isdigit():
            return False
        exp = int(exp_s)
        t = now if now is not None else time.time()
        if not t < exp <= t + SESSION_TTL_S:
            return False
        mac = hmac.new(self._session, f"owner|{exp}".encode(), hashlib.sha256).digest()
        return hmac.compare_digest(b64u(mac), sig)

    def csrf(self, session: str) -> str:
        return hmac.new(self._session, f"csrf|{session}".encode(), hashlib.sha256).hexdigest()[:32]


def pin_matches(given: str, expected: str) -> bool:
    a = hashlib.sha256(given.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)
