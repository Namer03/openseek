"""OSP1 (SR-1, SR-2): shared vectors and window rules (clock injected)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openseek import pairing as P

VECTORS = json.loads((Path(__file__).parent / "vectors" / "osp1.json").read_text(encoding="utf-8"))["vectors"]
PIN = VECTORS[0]["spki_pin"]


@pytest.mark.parametrize("v", VECTORS, ids=[v["description"][:30] for v in VECTORS])
def test_pair_proof_vectors(v):
    assert P.normalize_code(v["code_typed"]) == v["code_norm"]
    k = P.derive_key(v["code_typed"], v["pair_id"], v["salt"], v["spki_pin"], v["iterations"])
    assert k.hex() == v["K_hex"]
    assert P.client_proof(k, v["pair_id"], v["device_name"]) == v["client_proof"]
    assert P.server_proof(k, v["pair_id"], v["token"]) == v["server_proof"]


def test_wrong_pin_gives_different_key():
    a, b = VECTORS[1], VECTORS[3]
    assert a["code_norm"] == b["code_norm"] and a["spki_pin"] != b["spki_pin"]
    assert a["K_hex"] != b["K_hex"] and a["client_proof"] != b["client_proof"]


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture()
def pm():
    devices = []

    def reg(name, th):
        devices.append((name, th))
        return f"{len(devices):016x}"

    c = Clock()
    m = P.PairingManager(PIN, reg, clock=c, iterations=1000)
    m.devices, m.c = devices, c
    return m


def claim_body(m: P.PairingManager, code: str, name="Shield", pin=PIN):
    st = m.start()
    k = P.derive_key(code, st["pair_id"], st["salt"], pin, st["iterations"])
    return st["pair_id"], name, P.client_proof(k, st["pair_id"], name), k


def test_code_format():
    for _ in range(200):
        c = P.new_code()
        assert len(c) == 8 and set(c) <= set(P.ALPHABET)
    assert len(P.ALPHABET) == 31 and not set("01ILO") & set(P.ALPHABET)
    assert P.display_code("ABCD2345") == "ABCD-2345"


def test_pair_requires_open_window(pm):
    with pytest.raises(P.PairingClosed):
        pm.start()
    with pytest.raises(P.PairingFailed):
        pm.claim("0" * 32, "x", "0" * 64)


def test_pair_success_token_hashed(pm):
    code, ttl = pm.open_window()
    assert ttl == 600
    pid, name, proof, k = claim_body(pm, code.lower().replace("-", " "))
    res = pm.claim(pid, name, proof)
    assert len(res["token"]) == 43 and len(res["device_id"]) == 16
    assert res["server_proof"] == P.server_proof(k, pid, res["token"])
    assert pm.devices == [("Shield", P.token_hash(res["token"]))]       # only the hash is handed on


def test_pair_code_single_use_and_replay_rejected(pm):
    code, _ = pm.open_window()
    pid, name, proof, _ = claim_body(pm, code)
    pm.claim(pid, name, proof)
    with pytest.raises(P.PairingFailed):
        pm.claim(pid, name, proof)                       # replay of the captured body
    assert not pm.is_open()


def test_pair_code_expiry(pm):
    code, _ = pm.open_window()
    pid, name, proof, _ = claim_body(pm, code)
    pm.c.t += 601
    with pytest.raises(P.PairingFailed):
        pm.claim(pid, name, proof)
    with pytest.raises(P.PairingClosed):
        pm.start()


def test_pair_attempt_cap_burns_window(pm):
    code, _ = pm.open_window()
    pid, name, proof, _ = claim_body(pm, code)
    for _ in range(5):
        with pytest.raises(P.PairingFailed):
            pm.claim(pid, name, "0" * 64)
        pm.c.t += 7                                       # stay under the claim rate limit
    with pytest.raises(P.PairingFailed):
        pm.claim(pid, name, proof)                        # correct proof after 5 wrong: burned
    assert pm.admin_status()["burned"] is True


def test_pair_wrong_pin_rejected(pm):
    code, _ = pm.open_window()
    st = pm.start()
    attacker_pin = VECTORS[3]["spki_pin"]                 # TV computed K over a MITM's key
    k = P.derive_key(code, st["pair_id"], st["salt"], attacker_pin, st["iterations"])
    with pytest.raises(P.PairingFailed):
        pm.claim(st["pair_id"], "Shield", P.client_proof(k, st["pair_id"], "Shield"))


def test_device_name_bound_into_proof(pm):
    code, _ = pm.open_window()
    pid, _, proof, _ = claim_body(pm, code, name="Shield")
    with pytest.raises(P.PairingFailed):
        pm.claim(pid, "Other", proof)


def test_claim_rate_limit(pm):
    pm.open_window()
    st = pm.start()
    for _ in range(4):
        with pytest.raises(P.PairingFailed):
            pm.claim(st["pair_id"] + "", "x", "1" * 64)
    pm.open_window()   # new window, the global limiter keeps counting
    st = pm.start()
    for _ in range(6):
        with pytest.raises((P.PairingFailed, P.RateLimited)):
            pm.claim(st["pair_id"], "x", "1" * 64)
    with pytest.raises(P.RateLimited):
        pm.claim(st["pair_id"], "x", "1" * 64)
    pm.c.t += 61
    with pytest.raises(P.PairingFailed):
        pm.claim(st["pair_id"], "x", "1" * 64)


def test_start_rate_limit(pm):
    pm.open_window()
    for _ in range(30):
        pm.start()
    with pytest.raises(P.RateLimited):
        pm.start()


def test_malformed_claims_fail_without_burning(pm):
    code, _ = pm.open_window()
    for args in (("x", "a", "0" * 64), ("0" * 32, "", "0" * 64), ("0" * 32, "a\nb", "0" * 64),
                 ("0" * 32, "a", "ZZ"), ("0" * 32, "a" * 65, "0" * 64)):
        with pytest.raises(P.PairingFailed):
            pm.claim(*args)
    pid, name, proof, _ = claim_body(pm, code)
    assert pm.claim(pid, name, proof)["token"]
