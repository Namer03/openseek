"""Regenerate tests/vectors/osp1.json (OSP1 test vectors shared with the Nuvio C client).

Deliberately independent of openseek.pairing: it spells the contract formulas out with hashlib
so the backend implementation is checked against them, not against itself.

    python tests/vectors/make_osp1.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path


def vector(desc, code_typed, pair_id, salt, spki_pin, device_name, token, iterations):
    code_norm = "".join(c for c in code_typed.upper() if c not in " -")
    kdf_salt = "OSP1" + pair_id + salt + spki_pin
    k = hashlib.pbkdf2_hmac("sha256", code_norm.encode("utf-8"), kdf_salt.encode("utf-8"), iterations, 32)
    token_sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cp = hmac.new(k, ("client" + pair_id + device_name).encode("utf-8"), hashlib.sha256).hexdigest()
    sp = hmac.new(k, ("server" + pair_id + token_sha).encode("utf-8"), hashlib.sha256).hexdigest()
    return {"description": desc, "code_typed": code_typed, "code_norm": code_norm, "iterations": iterations,
            "pair_id": pair_id, "salt": salt, "spki_pin": spki_pin, "device_name": device_name,
            "device_name_utf8_hex": device_name.encode("utf-8").hex(), "token": token,
            "kdf_password_utf8": code_norm, "kdf_salt_utf8": kdf_salt, "token_sha256_hex": token_sha,
            "K_hex": k.hex(), "client_proof": cp, "server_proof": sp}


def main() -> None:
    pin = "sha256//" + base64.b64encode(hashlib.sha256(b"openseek-test-spki").digest()).decode()
    pin2 = "sha256//" + base64.b64encode(hashlib.sha256(b"attacker-spki").digest()).decode()
    tok = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode()
    pid = bytes(range(16)).hex()
    salt = bytes(range(16, 32)).hex()
    vs = [
        vector("production parameters", "K7QF-M2XD", pid, salt, pin, "Shield", tok, 600_000),
        vector("typed lowercase with space; UTF-8 device name; fast iterations for unit tests",
               "k7qf m2xd", pid, salt, pin, "Wohnzimmer TV ü", tok, 1_000),
        vector("minimum client-accepted iterations", "2345-6789", "f" * 32, "0" * 32, pin,
               "NVIDIA SHIELD Android TV", tok, 200_000),
        vector("same code over a different (attacker) pin -> different K", "K7QF-M2XD", pid, salt, pin2, "Shield",
               tok, 1_000),
    ]
    doc = {"protocol": "OSP1", "contract": "docs/api/CONTRACT-v1.md",
           "formulas": {
               "code_norm": "uppercase(code) with spaces/dashes removed",
               "K": "PBKDF2-HMAC-SHA256(code_norm, 'OSP1' || pair_id || salt || spki_pin, iterations, 32)",
               "client_proof": "hex(HMAC-SHA256(K, 'client' || pair_id || device_name))",
               "server_proof": "hex(HMAC-SHA256(K, 'server' || pair_id || hex(SHA-256(token))))",
               "encoding": "all strings UTF-8; pair_id/salt are the lowercase hex strings; hex output lowercase"},
           "vectors": vs}
    out = Path(__file__).with_name("osp1.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
