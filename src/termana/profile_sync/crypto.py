"""Authenticated sync envelopes using installed library primitives, not a cipher.

    magic | random HKDF salt (32) | random GCM nonce (12) | ciphertext + tag (16)

The raw 32-byte master must be random, not a password or GitHub token. Its vault,
recovery transfer and rotation UX are intentionally not implemented here. No
key is automatically created, logged, persisted or sent anywhere by this module.
"""

import os
from dataclasses import dataclass
from uuid import UUID

from termana.profile_sync.model import (
    MAX_SYNC_BYTES,
    SyncError,
    SyncSnapshot,
    decode_snapshot,
    encode_snapshot,
)

MAGIC = b"TERMANA-SYNC\x00\x01"
_SALT_BYTES = 32
_NONCE_BYTES = 12
_HEADER_BYTES = len(MAGIC) + _SALT_BYTES + _NONCE_BYTES
MAX_ENVELOPE_BYTES = _HEADER_BYTES + MAX_SYNC_BYTES + 16


@dataclass(frozen=True, slots=True)
class SyncContext:
    """Trusted onboarding binding, NOT values accepted from an incoming blob.

    A verified numeric GitHub repository ID remains stable across repo renames.
    Test contexts are synthetic; selecting/authorizing a real repo is later work.
    """

    repository_id: int
    catalog_id: UUID

    def __post_init__(self):
        if type(self.repository_id) is not int or not 1 <= self.repository_id <= 2**53 - 1:
            raise SyncError("Invalid sync repository identity")
        if type(self.catalog_id) is not UUID or not self.catalog_id.int:
            raise SyncError("Invalid sync catalog identity")

    def associated_data(self):
        return MAGIC + str(self.repository_id).encode("ascii") + b"\x00" + self.catalog_id.bytes


def _validate_inputs(key, context):
    if type(key) is not bytes or len(key) != 32 or type(context) is not SyncContext:
        raise SyncError("A 32-byte sync key and trusted context are required")


def _cipher(key, salt, context):
    _validate_inputs(key, context)
    try:
        from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        raise SyncError("Sync encryption is unavailable") from None
    try:
        derived = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=salt, info=context.associated_data()
        ).derive(key)
        return AESGCM(derived), InvalidTag
    except UnsupportedAlgorithm:
        raise SyncError("Sync encryption is unavailable") from None


def encrypt_snapshot(snapshot: SyncSnapshot, key: bytes, context: SyncContext) -> bytes:
    _validate_inputs(key, context)
    if (
        type(snapshot) is not SyncSnapshot
        or type(context) is not SyncContext
        or snapshot.catalog_id != context.catalog_id
    ):
        raise SyncError("Sync snapshot does not match its trusted context")
    payload = encode_snapshot(snapshot)
    try:
        salt, nonce = os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)
    except OSError:
        raise SyncError("Secure sync randomness is unavailable") from None
    cipher, _invalid_tag = _cipher(key, salt, context)
    header = MAGIC + salt + nonce
    return header + cipher.encrypt(nonce, payload, context.associated_data() + header)


def decrypt_snapshot(envelope: bytes, key: bytes, context: SyncContext) -> SyncSnapshot:
    if type(envelope) is not bytes or not _HEADER_BYTES + 16 < len(envelope) <= MAX_ENVELOPE_BYTES:
        raise SyncError("Invalid encrypted sync data size")
    if not envelope.startswith(MAGIC):
        raise SyncError("Unsupported encrypted sync format")
    salt = envelope[len(MAGIC) : len(MAGIC) + _SALT_BYTES]
    nonce = envelope[len(MAGIC) + _SALT_BYTES : _HEADER_BYTES]
    cipher, invalid_tag = _cipher(key, salt, context)
    try:
        payload = cipher.decrypt(
            nonce, envelope[_HEADER_BYTES:], context.associated_data() + envelope[:_HEADER_BYTES]
        )
    except invalid_tag:
        raise SyncError("Sync data could not be authenticated") from None
    snapshot = decode_snapshot(payload)
    if snapshot.catalog_id != context.catalog_id:
        raise SyncError("Sync snapshot does not match its trusted context")
    return snapshot
