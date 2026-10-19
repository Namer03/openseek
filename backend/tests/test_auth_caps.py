"""SR-5 token handling and SR-7 capability signatures (unit level)."""
from __future__ import annotations

import pytest

from openseek.auth import CapSigner, DeviceAuth
from openseek.db import DB
from openseek.pairing import new_token, token_hash

DEV, PID, PID2 = "0123456789abcdef", "a" * 32, "b" * 32


def test_cap_roundtrip_and_tamper():
    t = [1_700_000_000.0]
    s = CapSigner(b"k" * 32, clock=lambda: t[0])
    exp = s.current_exp()
    assert t[0] < exp <= t[0] + 6 * 3600
    cap = s.cap(DEV, PID, exp)
    assert len(cap) == 32
    assert s.verify(cap, DEV, PID, exp)
    flipped = cap[:5] + ("A" if cap[5] != "A" else "B") + cap[6:]
    assert not s.verify(flipped, DEV, PID, exp)                   # test_cap_tampered_403
    assert not s.verify(cap, DEV, PID2, exp)                      # test_cap_other_job_403
    assert not s.verify(cap, "f" * 16, PID, exp)
    assert not s.verify(cap, DEV, PID, exp + 1)


def test_cap_expiry_and_max_ttl():
    t = [1_700_000_000.0]
    s = CapSigner(b"k" * 32, clock=lambda: t[0])
    exp = s.current_exp()
    cap = s.cap(DEV, PID, exp)
    t[0] = exp                                                   # test_cap_expired_403
    assert not s.verify(cap, DEV, PID, exp)
    far = int(t[0]) + 7 * 3600                                    # validly signed but too far ahead
    assert not s.verify(s.cap(DEV, PID, far), DEV, PID, far)


def test_cap_stable_within_the_hour():
    t = [1_700_000_000.0]
    s = CapSigner(b"k" * 32, clock=lambda: t[0])
    e1 = s.current_exp()
    t[0] += 30
    assert s.current_exp() == e1                                  # tile URLs stay byte-identical


def test_token_hash_only_and_revocation(tmp_path):
    db = DB(tmp_path / "x.db")
    auth = DeviceAuth(db)
    tok = new_token()
    dev = auth.register("Shield", token_hash(tok))
    assert auth.authenticate(f"Bearer {tok}") == dev
    assert auth.authenticate(f"Bearer {new_token()}") is None
    assert auth.authenticate(tok) is None and auth.authenticate("Bearer ") is None
    assert auth.authenticate("Bearer " + tok[:-1] + "!") is None
    db.close()
    raw = (tmp_path / "x.db").read_bytes() + b"".join(p.read_bytes() for p in tmp_path.glob("x.db-*"))
    assert tok.encode() not in raw and token_hash(tok).encode() in raw
    db = DB(tmp_path / "x.db")
    auth = DeviceAuth(db)
    assert auth.authenticate(f"Bearer {tok}") == dev
    assert auth.revoke(dev)
    assert auth.authenticate(f"Bearer {tok}") is None
    db.close()


def test_short_secret_refused():
    with pytest.raises(ValueError):
        CapSigner(b"short")
