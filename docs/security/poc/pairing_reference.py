#!/usr/bin/env python3
"""OpenSeek pairing protocol v1 (OSP1) - REFERENCE + LOCALHOST DEMO, not production code.

Defines the exact byte transcript of SECURITY-REQUIREMENTS.md section A so the
Python backend and the C client (Nuvio) can be checked against the same test
vectors. Stdlib only.

  python -I pairing_reference.py vectors          print deterministic test vectors
  python -I pairing_reference.py demo CERT KEY    run a 127.0.0.1 TLS server with
                                                  CERT/KEY, pair a client against it
                                                  with an unpinned "start" then a
                                                  PINNED "claim", then show that a
                                                  wrong pin and a wrong code fail.
                                                  If curl is on PATH it also proves
                                                  that -k (VERIFYPEER off) +
                                                  --pinnedpubkey still enforces the pin.

Make a test cert:  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
                     -nodes -keyout k.pem -out c.pem -days 7300 -subj /CN=openseek
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # 31 symbols, no 0/o/1/l/i (as src/celular.c)
CODE_LEN = 8                                     # 31^8 = 8.5e11 (~39.6 bits)
KDF_ITER = 600_000
KDF_ITER_MIN = 200_000                           # client refuses less (downgrade guard)
KDF_ITER_MAX = 5_000_000                         # client refuses more (TV DoS guard)
LABEL_KDF = b"openseek-pair-v1\x00"
LABEL_C = b"OSP1-client"
LABEL_S = b"OSP1-server"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_code() -> str:
    out = []
    while len(out) < CODE_LEN:
        b = os.urandom(1)[0]
        if b < 248:                                   # rejection sampling, 248 = 8*31
            out.append(ALPHABET[b % 31])
    return "".join(out)


def display_code(code: str) -> str:
    return (code[:4] + "-" + code[4:]).upper()


def normalize_code(typed: str) -> str | None:
    c = "".join(ch for ch in typed.lower() if ch not in " -_.")
    return c if len(c) == CODE_LEN and all(ch in ALPHABET for ch in c) else None


# --------------------------------------------------------------- DER / SPKI pin
def _tlv(buf: bytes, i: int) -> tuple[int, int, int]:
    """Return (tag, start_of_value, end_of_value) for the TLV at i."""
    tag = buf[i]; i += 1
    ln = buf[i]; i += 1
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(buf[i:i + n], "big"); i += n
    return tag, i, i + ln


def spki_der_from_cert_der(cert: bytes) -> bytes:
    _, v, _ = _tlv(cert, 0)                # Certificate SEQUENCE
    _, tv, _ = _tlv(cert, v)               # tbsCertificate SEQUENCE
    i = tv
    tag, _, end = _tlv(cert, i)
    if tag == 0xA0:                        # [0] version
        i = end
    for _ in range(5):                     # serial, sigAlg, issuer, validity, subject
        _, _, end = _tlv(cert, i); i = end
    _, _, end = _tlv(cert, i)              # subjectPublicKeyInfo
    return cert[i:end]


def spki_sha256(cert_der: bytes) -> bytes:
    return hashlib.sha256(spki_der_from_cert_der(cert_der)).digest()


def curl_pin(spki_hash: bytes) -> str:
    """Exact CURLOPT_PINNEDPUBLICKEY format (standard base64, with padding)."""
    return "sha256//" + base64.b64encode(spki_hash).decode()


# --------------------------------------------------------------- transcript
def derive_key(code: str, pair_id: bytes, salt: bytes, spki_hash: bytes, it: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), LABEL_KDF + pair_id + salt + spki_hash, it, 32)


def client_proof(k: bytes, pair_id: bytes, nonce: bytes, spki_hash: bytes) -> bytes:
    return hmac.new(k, LABEL_C + pair_id + nonce + spki_hash, hashlib.sha256).digest()


def server_proof(k: bytes, pair_id: bytes, nonce: bytes, spki_hash: bytes, token: str) -> bytes:
    th = hashlib.sha256(token.encode()).digest()
    return hmac.new(k, LABEL_S + pair_id + nonce + spki_hash + th, hashlib.sha256).digest()


def vectors() -> None:
    code = "k7qfm2xd"
    pair_id = bytes(range(16)); salt = bytes(range(16, 32)); nonce = bytes(range(32, 48))
    spki = hashlib.sha256(b"test-spki").digest()
    token = "osk_" + b64u(bytes(range(48, 80)))
    for it in (1000, KDF_ITER):
        k = derive_key(code, pair_id, salt, spki, it)
        print(json.dumps({
            "code_typed": "K7QF-M2XD", "code_norm": code, "iter": it,
            "pair_id": b64u(pair_id), "salt": b64u(salt), "nonce": b64u(nonce),
            "spki_sha256_b64": base64.b64encode(spki).decode(), "token": token,
            "K_hex": k.hex(),
            "client_proof": b64u(client_proof(k, pair_id, nonce, spki)),
            "server_proof": b64u(server_proof(k, pair_id, nonce, spki, token)),
        }, indent=1))


# --------------------------------------------------------------- demo server
class PairState:
    def __init__(self, spki: bytes):
        self.spki = spki
        self.code = new_code()
        self.pair_id = os.urandom(16)
        self.salt = os.urandom(16)
        self.failures = 0
        self.used = False
        self.tokens: dict[str, str] = {}      # sha256(token) hex -> device name


def make_handler(st: PairState):
    class H(http.server.BaseHTTPRequestHandler):
        def _json(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                return self._json(413, {"error": "too_large"})
            req = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/v1/pair/start":
                if st.used:
                    return self._json(404, {"error": "no_pairing_window"})
                return self._json(200, {"v": 1, "pair_id": b64u(st.pair_id), "salt": b64u(st.salt),
                                        "spki": curl_pin(st.spki),
                                        "kdf": {"alg": "pbkdf2-sha256", "iter": KDF_ITER}})
            if self.path == "/v1/pair/claim":
                if st.used or req.get("pair_id") != b64u(st.pair_id):
                    return self._json(404, {"error": "no_pairing_window"})
                nonce = unb64u(req["nonce"])
                k = derive_key(st.code, st.pair_id, st.salt, st.spki, KDF_ITER)
                if not hmac.compare_digest(client_proof(k, st.pair_id, nonce, st.spki), unb64u(req["proof"])):
                    st.failures += 1
                    if st.failures >= 5:
                        st.used = True               # window burned; PC shows a warning
                    return self._json(403, {"error": "bad_code"})
                st.used = True                       # single use
                token = "osk_" + b64u(os.urandom(32))
                st.tokens[hashlib.sha256(token.encode()).hexdigest()] = req.get("device_name", "?")[:64]
                return self._json(200, {"token": token,
                                        "proof": b64u(server_proof(k, st.pair_id, nonce, st.spki, token))})
            self._json(404, {"error": "not_found"})

        def log_message(self, *a):
            pass                                     # never log request lines (tokens/codes)
    return H


def tls_post(port: int, path: str, obj: dict, pin: bytes | None) -> tuple[int, dict]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                  # == VERIFYPEER 0 / VERIFYHOST 0
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname="openseek") as s:
            if pin is not None and not hmac.compare_digest(spki_sha256(s.getpeercert(True)), pin):
                raise ssl.SSLError("pin mismatch")   # == CURLE_SSL_PINNEDPUBKEYNOTMATCH (90)
            body = json.dumps(obj).encode()
            s.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload or b"{}")


def client_pair(port: int, typed_code: str, wrong_pin: bool = False) -> str:
    code = normalize_code(typed_code)
    if not code:
        raise ValueError("bad code format")
    st, start = tls_post(port, "/v1/pair/start", {"v": 1}, pin=None)   # unpinned, carries no secret
    if st != 200:
        raise RuntimeError(f"start refused HTTP {st} {start}")
    it = start["kdf"]["iter"]
    if not (KDF_ITER_MIN <= it <= KDF_ITER_MAX):
        raise RuntimeError("kdf downgrade refused")
    spki = base64.b64decode(start["spki"][len("sha256//"):])
    pair_id, salt, nonce = unb64u(start["pair_id"]), unb64u(start["salt"]), os.urandom(16)
    k = derive_key(code, pair_id, salt, spki, it)
    pin = hashlib.sha256(b"attacker").digest() if wrong_pin else spki
    st, res = tls_post(port, "/v1/pair/claim",
                       {"v": 1, "pair_id": start["pair_id"], "nonce": b64u(nonce),
                        "proof": b64u(client_proof(k, pair_id, nonce, spki)), "device_name": "SHIELD"},
                       pin=pin)
    if st != 200:
        raise RuntimeError(f"claim refused HTTP {st} {res}")
    if not hmac.compare_digest(server_proof(k, pair_id, nonce, spki, res["token"]), unb64u(res["proof"])):
        raise RuntimeError("server proof invalid - possible interception; token discarded")
    return res["token"]


def demo(cert: str, key: str) -> None:
    der = ssl.PEM_cert_to_DER_cert(open(cert).read())
    spki = spki_sha256(der)
    print("server pin:", curl_pin(spki))

    def start_server():
        st = PairState(spki)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st))
        srv.handle_error = lambda *a: None          # client aborts on pin mismatch: expected
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.minimum_version = ssl.TLSVersion.TLSv1_2
        sctx.load_cert_chain(cert, key)
        srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return st, srv

    st, srv = start_server()
    port = srv.server_address[1]
    print("PC shows code:", display_code(st.code))
    tok = client_pair(port, display_code(st.code))
    print("1 correct code + correct pin -> token", tok[:8] + "...", "(stored hashed:", len(st.tokens), ")")
    try:
        client_pair(port, display_code(st.code)); print("2 FAIL replay accepted")
    except Exception as e:
        print("2 replay of a used code       ->", e)
    srv.shutdown()

    st, srv = start_server(); port = srv.server_address[1]
    try:
        client_pair(port, display_code(st.code), wrong_pin=True); print("3 FAIL wrong pin accepted")
    except Exception as e:
        print("3 correct code, wrong pin     ->", type(e).__name__, e)
    srv.shutdown()

    st, srv = start_server(); port = srv.server_address[1]
    for i in range(6):
        try:
            client_pair(port, "AAAA-AAAB")
        except Exception as e:
            last = e
    print("4 six wrong codes             ->", last)
    try:
        client_pair(port, display_code(st.code)); print("5 FAIL burned window accepted the right code")
    except Exception as e:
        print("5 right code after lockout    ->", e)
    srv.shutdown()

    curl = shutil.which("curl")
    if curl:
        st, srv = start_server(); port = srv.server_address[1]
        url = f"https://127.0.0.1:{port}/v1/pair/start"
        for label, pin in (("right", curl_pin(spki)), ("wrong", curl_pin(hashlib.sha256(b"x").digest()))):
            p = subprocess.run([curl, "-sk", "-o", os.devnull, "-w", "%{http_code}", "-X", "POST",
                                "-H", "Content-Type: application/json", "-d", "{}",
                                "--pinnedpubkey", pin, url], capture_output=True, text=True)
            print(f"6 curl -k --pinnedpubkey {label:<5} -> exit={p.returncode} http={p.stdout}")
        srv.shutdown()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "vectors":
        vectors()
    elif len(sys.argv) >= 4 and sys.argv[1] == "demo":
        demo(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
