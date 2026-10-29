"""Bounded actual-connection X509 details. No TLS implementation or CA bypass."""
import hashlib
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding


def display_fingerprint(fingerprint):
    """Four readable eight-byte rows; never impose a 95-character label minimum."""
    return "\n".join(":".join(fingerprint[i:i+2] for i in range(start,start+16,2))
                     for start in range(0,64,16))


@dataclass(frozen=True, slots=True)
class RdpCertificate:
    fingerprint: str
    flags: int
    not_before: datetime
    not_after: datetime
    der: bytes = field(repr=False)

    def currently_valid(self, now=None):
        now = datetime.now(UTC) if now is None else now
        return self.not_before <= now <= self.not_after

    @property
    def name_mismatch(self):
        return bool(self.flags & 128)

    @property
    def remember_allowed(self):
        # No gateway, redirection, legacy transport or unknown flags are approved.
        return not self.flags & ~0x2C0 and self.currently_valid()


def decode_certificate(payload):
    if type(payload) is not bytes or not 72 < len(payload) <= 16384:
        raise ValueError("Invalid RDP certificate details")
    flags = struct.unpack_from("!I", payload)[0]
    fingerprint = payload[4:68].decode("ascii")
    length = struct.unpack_from("!I", payload, 68)[0]
    der = payload[72:]
    if length != len(der) or fingerprint != hashlib.sha256(der).hexdigest():
        raise ValueError("Invalid RDP certificate identity")
    try:
        cert = x509.load_der_x509_certificate(der)
        if cert.public_bytes(Encoding.DER) != der:
            raise ValueError()
        before, after = cert.not_valid_before_utc, cert.not_valid_after_utc
        if before >= after:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Invalid RDP certificate details") from None
    return RdpCertificate(fingerprint, flags, before, after, der)
