"""Explicit remote protection formats. Unknown/corrupt ciphertext never falls back."""

from termana.profile_sync.crypto import MAGIC, MAX_ENVELOPE_BYTES
from termana.profile_sync.model import (
    MAX_SYNC_BYTES,
    SyncError,
    canonical_json,
    decode_snapshot,
    encode_snapshot,
    parse_json,
)

MAX_CATALOG_BYTES = max(MAX_ENVELOPE_BYTES, MAX_SYNC_BYTES + 256)


def encrypted_format(payload):
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_CATALOG_BYTES:
        raise SyncError("Invalid sync data size")
    if payload.startswith(MAGIC):
        return True
    # Recognize ONLY our strict plaintext wrapper, not arbitrary JSON or damaged
    # encrypted data. The caller still validates the trusted repository/catalog.
    _plain_document(payload)
    return False


def _plain_document(payload):
    data = parse_json(payload, limit=MAX_CATALOG_BYTES)
    if (
        type(data) is not dict
        or set(data) != {"format", "version", "repository_id", "snapshot"}
        or data["format"] != "termana-sync-plain"
        or type(data["version"]) is not int
        or data["version"] != 1
        or type(data["repository_id"]) is not int
        or not 0 < data["repository_id"] <= 2**53 - 1
    ):
        raise SyncError("Unsupported sync format; no data was replaced")
    return data


def read_plain(payload, repository_id, catalog_id=None):
    data = _plain_document(payload)
    if data["repository_id"] != repository_id:
        raise SyncError("Plaintext catalog belongs to a different repository")
    snapshot = decode_snapshot(canonical_json(data["snapshot"]))
    if catalog_id is not None and snapshot.catalog_id != catalog_id:
        raise SyncError("Plaintext catalog identity changed")
    return snapshot


def write_plain(snapshot, repository_id):
    result = canonical_json(
        {
            "format": "termana-sync-plain",
            "version": 1,
            "repository_id": repository_id,
            "snapshot": parse_json(encode_snapshot(snapshot)),
        }
    )
    if len(result) > MAX_CATALOG_BYTES:
        raise SyncError("Sync data exceeds its size limit")
    return result
