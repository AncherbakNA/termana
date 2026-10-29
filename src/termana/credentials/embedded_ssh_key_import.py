"""Bound actual OpenSSH private material before the pinned library constructs a key.

Only bounded parsing/KDF/decryption happens here. AsyncSSH/PyCA still performs
private-key validation; no global decoder patch or skipped RSA validation.
Call import_bounded_private_key only off the GUI/asyncio thread.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

import asyncssh
from asyncssh.encryption import get_encryption, get_encryption_params
from asyncssh.packet import PacketDecodeError, SSHPacket, String, UInt32
from cryptography.hazmat.primitives.asymmetric import ec

MAX_EMBEDDED_KEY_FILE_BYTES: Final = 1024 * 1024
MAX_EMBEDDED_KEY_BCRYPT_ROUNDS: Final = 256
MAX_EMBEDDED_RSA_MODULUS_BITS: Final = 8192
MAX_EMBEDDED_RSA_MODULUS_BYTES: Final = MAX_EMBEDDED_RSA_MODULUS_BITS // 8 + 1
MISSING_EMBEDDED_KEY_PASSPHRASE: Final = (
    "Passphrase must be specified to import encrypted private keys"
)
_KEY_HEADER: Final = b"-----BEGIN OPENSSH PRIVATE KEY-----"
_KEY_FOOTER: Final = b"-----END OPENSSH PRIVATE KEY-----"
_KEY_MAGIC: Final = b"openssh-key-v1\0"
_KEY_CIPHERS: Final = frozenset(
    b"aes" + bits + mode for bits in (b"128", b"192", b"256") for mode in (b"-cbc", b"-ctr")
)
_CURVE_BITS: Final = MappingProxyType({b"nistp256": 256, b"nistp384": 384, b"nistp521": 521})


class EmbeddedIdentityError(RuntimeError):
    """Fixed diagnostics only; never include key data, paths or passphrases."""

    def __init__(self) -> None:
        super().__init__("The selected SSH identity could not be used.")


@dataclass(frozen=True, slots=True)
class _KeyEnvelope:
    algorithm: bytes
    public_data: bytes
    parameters: tuple[bytes, ...]
    key_data: bytes = field(repr=False)
    cipher: bytes
    salt: bytes
    rounds: int


def _positive_mpint(data: bytes, bits: int) -> int:
    """Check length before integer conversion, including the positive mpint prefix."""
    if not 1 <= len(data) <= (bits + 7) // 8 + 1:
        raise EmbeddedIdentityError()
    value = int.from_bytes(data, "big", signed=True)
    if value <= 0 or value.bit_length() > bits:
        raise EmbeddedIdentityError()
    return value


def _public_parameters(packet: SSHPacket, algorithm: bytes) -> tuple[bytes, ...]:
    if algorithm == b"ssh-rsa":
        exponent, modulus = packet.get_string(), packet.get_string()
        _positive_mpint(exponent, 32)
        _positive_mpint(modulus, MAX_EMBEDDED_RSA_MODULUS_BITS)
        result = (exponent, modulus)
    elif algorithm == b"ssh-ed25519":
        public = packet.get_string()
        if len(public) != 32:
            raise EmbeddedIdentityError()
        result = (public,)
    elif algorithm.startswith(b"ecdsa-sha2-"):
        curve, point = packet.get_string(), packet.get_string()
        bits = _CURVE_BITS.get(curve)
        if (
            bits is None
            or algorithm != b"ecdsa-sha2-" + curve
            or not 1 <= len(point) <= 1 + 2 * ((bits + 7) // 8)
        ):
            raise EmbeddedIdentityError()
        result = (curve, point)
    else:
        raise EmbeddedIdentityError()
    packet.check_end()
    return result


def _parse_key_envelope(data: bytes) -> _KeyEnvelope:
    if type(data) is not bytes or not 0 < len(data) <= MAX_EMBEDDED_KEY_FILE_BYTES:
        raise EmbeddedIdentityError()
    try:
        lines = data.strip().splitlines()
        if len(lines) < 3 or lines[0] != _KEY_HEADER or lines[-1] != _KEY_FOOTER:
            raise EmbeddedIdentityError()
        binary = base64.b64decode(b"".join(lines[1:-1]), validate=True)
        if not binary.startswith(_KEY_MAGIC):
            raise EmbeddedIdentityError()
        envelope = SSHPacket(binary[len(_KEY_MAGIC) :])
        cipher, kdf, kdf_data = [envelope.get_string() for _ in range(3)]
        if envelope.get_uint32() != 1:
            raise EmbeddedIdentityError()
        public_data, key_data = envelope.get_string(), envelope.get_string()
        envelope.check_end()  # AEAD/tagged formats intentionally stay on system SSH.
        public = SSHPacket(public_data)
        algorithm = public.get_string()
        parameters = _public_parameters(public, algorithm)
        salt, rounds = b"", 0
        if cipher == b"none":
            if kdf != b"none" or kdf_data:
                raise EmbeddedIdentityError()
        elif cipher in _KEY_CIPHERS and kdf == b"bcrypt":
            kdf_packet = SSHPacket(kdf_data)
            salt, rounds = kdf_packet.get_string(), kdf_packet.get_uint32()
            kdf_packet.check_end()
            if not 1 <= len(salt) <= 64 or not 1 <= rounds <= MAX_EMBEDDED_KEY_BCRYPT_ROUNDS:
                raise EmbeddedIdentityError()
        else:
            raise EmbeddedIdentityError()
        return _KeyEnvelope(algorithm, public_data, parameters, key_data, cipher, salt, rounds)
    except (ValueError, PacketDecodeError):
        raise EmbeddedIdentityError() from None


def validate_embedded_key_envelope(data: bytes) -> None:
    """Preflight the public envelope and declared KDF, without constructing a key."""
    _parse_key_envelope(data)


def _validate_private_material(envelope: _KeyEnvelope, data: bytes) -> None:
    """Check the real private algorithm/fields before crypto constructors or RSA modulo."""
    packet = SSHPacket(data)
    if packet.get_uint32() != packet.get_uint32():
        if envelope.cipher != b"none":
            raise asyncssh.KeyEncryptionError("Incorrect passphrase")
        raise EmbeddedIdentityError()
    if packet.get_string() != envelope.algorithm:
        raise EmbeddedIdentityError()
    public = envelope.parameters
    if envelope.algorithm == b"ssh-rsa":
        # OpenSSH order is n,e,d,iqmp,p,q. Bound every number, not just n.
        numbers = tuple(
            _positive_mpint(packet.get_string(), bits)
            for bits in (MAX_EMBEDDED_RSA_MODULUS_BITS, 32) + (MAX_EMBEDDED_RSA_MODULUS_BITS,) * 4
        )
        if (
            numbers[0] != _positive_mpint(public[1], MAX_EMBEDDED_RSA_MODULUS_BITS)
            or numbers[1] != _positive_mpint(public[0], 32)
            or numbers[4] < 2
            or numbers[5] < 2
        ):
            raise EmbeddedIdentityError()
    elif envelope.algorithm == b"ssh-ed25519":
        declared, private = packet.get_string(), packet.get_string()
        if declared != public[0] or len(private) != 64 or private[32:] != declared:
            raise EmbeddedIdentityError()
    else:
        curve, point = packet.get_string(), packet.get_string()
        if (curve, point) != public:
            raise EmbeddedIdentityError()
        _positive_mpint(packet.get_string(), _CURVE_BITS[curve])
    packet.get_string()  # Comment remains bounded by the complete selected-file limit.
    padding = packet.get_remaining_payload()
    if len(padding) >= 256 or padding != bytes(range(1, len(padding) + 1)):
        raise EmbeddedIdentityError()


def import_bounded_private_key(data: bytes, passphrase: str | None = None) -> asyncssh.SSHKey:
    """Decrypt once using pinned primitives; require full library validation afterwards."""
    envelope = _parse_key_envelope(data)
    private_data = envelope.key_data
    try:
        if envelope.cipher != b"none":
            if passphrase is None:
                raise asyncssh.KeyImportError(MISSING_EMBEDDED_KEY_PASSPHRASE)
            # Bcrypt is an optional dependency of the explicit embedded extra.
            import bcrypt

            key_size, iv_size, *_ = get_encryption_params(envelope.cipher)
            try:
                material = bcrypt.kdf(
                    passphrase.encode("utf-8"),
                    envelope.salt,
                    key_size + iv_size,
                    envelope.rounds,
                    ignore_few_rounds=True,
                )
            except ValueError:
                raise asyncssh.KeyEncryptionError("Invalid OpenSSH private key") from None
            cipher = get_encryption(envelope.cipher, material[:key_size], material[key_size:])
            private_data = cipher.decrypt_packet(0, b"", private_data, 0, b"")
            if private_data is None:
                raise asyncssh.KeyEncryptionError("Incorrect passphrase")
        _validate_private_material(envelope, private_data)
        # Reuse the full reviewed importer on unencrypted, already bounded
        # material, so KDF work is not repeated and validation is not skipped.
        plain = (
            _KEY_MAGIC
            + String(b"none")
            + String(b"none")
            + String(b"")
            + UInt32(1)
            + String(envelope.public_data)
            + String(private_data)
        )
        encoded = _KEY_HEADER + b"\n" + base64.b64encode(plain) + b"\n" + _KEY_FOOTER + b"\n"
        key = asyncssh.import_private_key(encoded, unsafe_skip_rsa_key_validation=False)
        if key.public_data != envelope.public_data:
            raise EmbeddedIdentityError()
        if envelope.algorithm.startswith(b"ecdsa-sha2-"):
            private = key.pyca_key
            if not isinstance(private, ec.EllipticCurvePrivateKey):
                raise EmbeddedIdentityError()
            declared = ec.EllipticCurvePublicKey.from_encoded_point(
                private.curve, envelope.parameters[1]
            )
            if declared.public_numbers() != private.public_key().public_numbers():
                raise EmbeddedIdentityError()
        return key
    except PacketDecodeError:
        raise EmbeddedIdentityError() from None
