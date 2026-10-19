"""Self-signed ECDSA P-256 server certificate (generated on first start) and its SPKI pin."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .cache import atomic_write, secure_mkdir


def spki_pin_from_cert_pem(pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(pem)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256//" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def spki_pin_from_cert_der(der: bytes) -> str:
    cert = x509.load_der_x509_certificate(der)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256//" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def ensure_certificate(data_dir: Path) -> tuple[Path, Path, str]:
    d = Path(data_dir) / "tls"
    secure_mkdir(d)
    cert_p, key_p = d / "cert.pem", d / "key.pem"
    if not (cert_p.exists() and key_p.exists()):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "openseek")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=7300))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("openseek")]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        atomic_write(key_p, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        atomic_write(cert_p, cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p, spki_pin_from_cert_pem(cert_p.read_bytes())
