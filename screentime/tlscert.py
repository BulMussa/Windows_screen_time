"""The phone-sync listener's self-signed TLS certificate, made with the "cryptography" package.

A P-256 key and a self-signed X.509 certificate, made on first use and kept as PEM files that
ssl.SSLContext.load_cert_chain reads. The phone doesn't trust it through a certificate authority:
the pairing QR code carries its SHA-256 fingerprint, which the phone pins. So it never needs to
name this PC's address, and it's valid for 20 years.
"""
import hashlib
import os
import socket
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_YEARS = 20


def make_certificate(common_name):
    """(certificate PEM, private key PEM) for a new P-256 key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64])])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365 * VALID_YEARS))
            # Its own CA, like "openssl req -x509", so a client can also trust it as a root
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def ensure_certificate(folder):
    """Paths (cert.pem, key.pem) in folder, made on first use. The key stays in this Windows user's
    own app data folder, like the logs."""
    cert_path, key_path = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        os.makedirs(folder, exist_ok=True)
        cert, key = make_certificate(f"Screen Time on {socket.gethostname()}")
        for path, data in ((key_path, key), (cert_path, cert)):  # key first: a cert without its key is useless
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
    return cert_path, key_path


def sha256(cert_path):
    """SHA-256 of the certificate (its DER bytes) as 64 lowercase hex digits: what the phone pins."""
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def fingerprint(cert_path):
    """The same SHA-256, written AB:CD:... for people to compare."""
    digest = sha256(cert_path).upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
