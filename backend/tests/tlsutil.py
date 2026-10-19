"""Throwaway test CA + leaf certificates (for vetted-TLS tests of the outbound fetcher)."""
from __future__ import annotations

import datetime as dt
import ssl
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


class TestCA:
    __test__ = False
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        ski = x509.SubjectKeyIdentifier.from_public_key(self.key.public_key())
        self.cert = (x509.CertificateBuilder().subject_name(_name("openseek test CA")).issuer_name(_name("openseek test CA"))
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                  content_commitment=False, key_encipherment=False,
                                                  data_encipherment=False, key_agreement=False,
                                                  encipher_only=False, decipher_only=False), critical=True)
                     .add_extension(ski, critical=False)
                     .sign(self.key, hashes.SHA256()))
        self.pem = self.cert.public_bytes(serialization.Encoding.PEM)
        self._dir = tempfile.TemporaryDirectory()

    def client_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cadata=self.pem.decode())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def server_context(self, *dns_names: str) -> ssl.SSLContext:
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(_name(dns_names[0])).issuer_name(self.cert.subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                               critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(self.key, hashes.SHA256()))
        d = Path(self._dir.name)
        cp, kp = d / f"{dns_names[0]}.crt", d / f"{dns_names[0]}.key"
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + self.pem)
        kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cp, kp)
        return ctx
