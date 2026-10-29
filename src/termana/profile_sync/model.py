"""Versioned, immutable, credential-free sync snapshots and local identity maps.

Portable import intentionally allocates new local IDs. This separate format
uses its own sync IDs instead; local store UUIDs and device bindings never go
on the wire. Nothing here reads or writes a user's connection store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from termana.models.connection import (
    MAX_CONNECTION_CATALOG_ITEMS,
    MAX_CONNECTION_FOLDER_DEPTH,
    ConnectionCatalog,
    ConnectionFolder,
)
from termana.storage.connection_transfer import (
    CONNECTION_TRANSFER_VERSION,
    ConnectionTransferError,
    decode_connection_transfer,
    encode_connection_catalog,
)

MAX_SYNC_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_TOMBSTONES = 50_000
_PROFILE_FIELDS = frozenset(
    {
        "type",
        "name",
        "protocol",
        "host",
        "port",
        "telnet_encoding",
        "ssh_cipher",
        "ssh_options",
        "is_favorite",
        "ssh_jump",
        "ssh_algorithms",
        "rdp_options",
        "word_delete_mode",
        "accent_color",
    }
)


class SyncError(ValueError):
    """Fixed diagnostics only: never reflect endpoints, data, keys or file paths."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SyncError("Duplicate sync field")
        result[key] = value
    return result


def _constant(_value):
    raise SyncError("Nonstandard JSON value in sync data")


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SyncError("Invalid sync document") from None


def parse_json(payload: bytes, *, limit: int = MAX_SYNC_BYTES):
    if type(payload) is not bytes or not 1 <= len(payload) <= limit:
        raise SyncError("Sync data exceeds its size limit")
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise SyncError("Invalid sync JSON") from None


def _valid_uuid(value):
    if type(value) is not UUID or not value.int:
        raise SyncError("Invalid sync identity")


def _uuid_from_text(value):
    try:
        result = UUID(value) if type(value) is str else None
    except ValueError:
        raise SyncError("Invalid sync identity") from None
    _valid_uuid(result)
    if str(result) != value:
        raise SyncError("Sync identities must use canonical UUID text")
    return result


def _ids(values, *, maximum=MAX_CONNECTION_CATALOG_ITEMS):
    if type(values) is not tuple or len(values) > maximum:
        raise SyncError("Invalid sync identity list")
    for item in values:
        _valid_uuid(item)
    if len(set(values)) != len(values):
        raise SyncError("Duplicate sync identity")


def _text_ids(values, *, maximum=MAX_CONNECTION_CATALOG_ITEMS):
    if type(values) is not list or len(values) > maximum:
        raise SyncError("Invalid sync identity list")
    result = tuple(_uuid_from_text(item) for item in values)
    _ids(result, maximum=maximum)
    return result


@dataclass(frozen=True, slots=True)
class SyncRecord:
    item_id: UUID
    kind: str
    data: bytes = field(repr=False)

    def __post_init__(self):
        _valid_uuid(self.item_id)
        if type(self.kind) is not str or self.kind not in {"folder", "connection"}:
            raise SyncError("Unsupported sync record kind")
        value = self.document()
        if type(value) is not dict:
            raise SyncError("Sync record must be an object")
        if self.kind == "folder":
            if set(value) != {"name", "children"}:
                raise SyncError("Unsupported sync folder fields")
            _text_ids(value["children"])
        else:
            if set(value) != _PROFILE_FIELDS or value["type"] != "connection":
                raise SyncError("Unsupported sync profile fields")
            # Ordinary portable12 exports a gateway username; sync deliberately
            # does NOT. Keep the codec's exact nullable shape, never its login.
            jump = value["ssh_jump"]
            if type(jump) is not dict or jump.get("username") is not None:
                raise SyncError("Sync must not contain gateway credentials")

    def document(self):
        """Return a fresh mutable view, never a mutable reference to stored data."""
        return parse_json(self.data, limit=MAX_RECORD_BYTES)


@dataclass(frozen=True, slots=True)
class SyncSnapshot:
    catalog_id: UUID
    roots: tuple[UUID, ...] = field(repr=False)
    records: tuple[SyncRecord, ...] = field(repr=False)
    tombstones: tuple[UUID, ...] = field(default=(), repr=False)

    def __post_init__(self):
        _valid_uuid(self.catalog_id)
        _ids(self.roots)
        _ids(self.tombstones, maximum=MAX_TOMBSTONES)
        if type(self.records) is not tuple or len(self.records) > MAX_CONNECTION_CATALOG_ITEMS:
            raise SyncError("Too many sync records")
        if any(type(item) is not SyncRecord for item in self.records):
            raise SyncError("Invalid sync record")
        records = {item.item_id: item for item in self.records}
        if len(records) != len(self.records) or set(records) & set(self.tombstones):
            raise SyncError("Duplicate live or deleted sync identity")
        seen = set()

        def visit(item_id, depth):
            if item_id in seen or item_id not in records:
                raise SyncError("Sync tree has a repeated or missing item")
            seen.add(item_id)
            item = records[item_id]
            data = item.document()
            if item.kind == "connection":
                return data
            if depth >= MAX_CONNECTION_FOLDER_DEPTH:
                raise SyncError("Sync folder tree is too deep")
            children = _text_ids(data["children"])
            return {
                "type": "folder",
                "name": data["name"],
                "items": [visit(child, depth + 1) for child in children],
            }

        portable = {
            "format": "termana-connections",
            "version": 12,
            "items": [visit(item_id, 0) for item_id in self.roots],
        }
        if seen != set(records):
            raise SyncError("Sync tree contains unreachable records")
        try:
            # Reuse the canonical field/protocol/folder validators, without
            # importing their newly allocated local IDs into sync identity.
            decoded = decode_connection_transfer(canonical_json(portable))
            if json.loads(encode_connection_catalog(decoded.catalog)) != portable:
                raise SyncError("Sync profile values are not canonical")
        except (ConnectionTransferError, TypeError, ValueError):
            raise SyncError("Sync tree or portable preferences are invalid") from None
        if len(encode_snapshot(self)) > MAX_SYNC_BYTES:
            raise SyncError("Sync data exceeds its size limit")


def encode_snapshot(snapshot: SyncSnapshot) -> bytes:
    if type(snapshot) is not SyncSnapshot:
        raise SyncError("Expected a validated sync snapshot")
    payload = canonical_json(
        {
            "format": "termana-profile-sync",
            "version": 1,
            "portable_version": 12,
            "catalog_id": str(snapshot.catalog_id),
            "roots": [str(item) for item in snapshot.roots],
            "records": [
                {"id": str(item.item_id), "kind": item.kind, "data": item.document()}
                for item in sorted(snapshot.records, key=lambda item: item.item_id)
            ],
            "tombstones": [str(item) for item in sorted(snapshot.tombstones)],
        }
    )
    if len(payload) > MAX_SYNC_BYTES:
        raise SyncError("Sync data exceeds its size limit")
    return payload


def decode_snapshot(payload: bytes) -> SyncSnapshot:
    data = parse_json(payload)
    fields = {
        "format",
        "version",
        "portable_version",
        "catalog_id",
        "roots",
        "records",
        "tombstones",
    }
    if type(data) is not dict or set(data) != fields:
        raise SyncError("Unsupported sync document fields")
    if (
        data["format"] != "termana-profile-sync"
        or type(data["version"]) is not int
        or data["version"] != 1
        or type(data["portable_version"]) is not int
        or data["portable_version"] != 12
    ):
        raise SyncError("Unsupported sync format version")
    raw = data["records"]
    if type(raw) is not list or len(raw) > MAX_CONNECTION_CATALOG_ITEMS:
        raise SyncError("Too many sync records")
    records = []
    for item in raw:
        if type(item) is not dict or set(item) != {"id", "kind", "data"}:
            raise SyncError("Unsupported sync record fields")
        records.append(
            SyncRecord(_uuid_from_text(item["id"]), item["kind"], canonical_json(item["data"]))
        )
    return SyncSnapshot(
        _uuid_from_text(data["catalog_id"]),
        _text_ids(data["roots"]),
        tuple(records),
        _text_ids(data["tombstones"], maximum=MAX_TOMBSTONES),
    )


@dataclass(frozen=True, slots=True)
class SyncBinding:
    local_id: UUID
    sync_id: UUID
    kind: str

    def __post_init__(self):
        _valid_uuid(self.local_id)
        _valid_uuid(self.sync_id)
        if (
            self.local_id == self.sync_id
            or type(self.kind) is not str
            or self.kind not in {"folder", "connection"}
        ):
            raise SyncError("Invalid local sync binding")


@dataclass(frozen=True, slots=True)
class SyncIdentities:
    """LOCAL ONLY. Persist atomically with a future approved catalog application.

    Retired IDs never expire automatically. Undo/recreation uses a NEW sync ID,
    so an offline device cannot mistake a resurrected object for an old deletion.
    """

    catalog_id: UUID = field(default_factory=uuid4)
    bindings: tuple[SyncBinding, ...] = field(default=(), repr=False)
    tombstones: tuple[UUID, ...] = field(default=(), repr=False)

    def __post_init__(self):
        _valid_uuid(self.catalog_id)
        _ids(self.tombstones, maximum=MAX_TOMBSTONES)
        if type(self.bindings) is not tuple or len(self.bindings) > MAX_CONNECTION_CATALOG_ITEMS:
            raise SyncError("Invalid local sync bindings")
        if any(type(item) is not SyncBinding for item in self.bindings):
            raise SyncError("Invalid local sync binding")
        local = {item.local_id for item in self.bindings}
        sync = {item.sync_id for item in self.bindings}
        if (
            len(local) != len(self.bindings)
            or len(sync) != len(self.bindings)
            or sync & set(self.tombstones)
            or local & sync
        ):
            raise SyncError("Local sync identities collide")


def project_catalog(
    catalog: ConnectionCatalog,
    identities: SyncIdentities,
) -> tuple[SyncSnapshot, SyncIdentities]:
    """Project one COMPLETE local catalog without reading stores or copying auth.

    The returned identities must be retained; generating a new map each time
    would allocate new IDs and is NOT synchronization. No map is persisted here.
    """
    if type(catalog) is not ConnectionCatalog or type(identities) is not SyncIdentities:
        raise SyncError("A complete catalog and local identity map are required")
    if CONNECTION_TRANSFER_VERSION != 12:
        raise SyncError("Portable field changes require a sync privacy review")
    try:
        portable = json.loads(encode_connection_catalog(catalog))
    except (ConnectionTransferError, ValueError, TypeError):
        raise SyncError("Could not project the portable connection catalog") from None
    previous = {item.local_id: item for item in identities.bindings}
    bindings, records = [], []
    native_ids = {item.folder_id for item in catalog.folders} | {
        item.profile_id for item in catalog.profiles
    }
    known_sync_ids = {item.sync_id for item in identities.bindings} | set(identities.tombstones)
    if identities.catalog_id in native_ids or native_ids & known_sync_ids:
        raise SyncError("Sync identities must be separate from local item identities")
    forbidden = (
        native_ids
        | {item.sync_id for item in identities.bindings}
        | set(identities.tombstones)
        | {
            identities.catalog_id,
        }
    )

    def fresh():
        for _attempt in range(128):
            candidate = uuid4()
            if type(candidate) is UUID and candidate.int and candidate not in forbidden:
                forbidden.add(candidate)
                return candidate
        raise SyncError("Could not allocate a unique sync identity")

    def walk(items, parent):
        roots = []
        local_items = catalog.children_of(parent)
        if len(items) != len(local_items):
            raise SyncError("Portable projection does not match the catalog")
        for data, local in zip(items, local_items, strict=True):
            folder = isinstance(local, ConnectionFolder)
            local_id = local.folder_id if folder else local.profile_id
            kind = "folder" if folder else "connection"
            old = previous.get(local_id)
            sync_id = old.sync_id if old is not None and old.kind == kind else fresh()
            bindings.append(SyncBinding(local_id, sync_id, kind))
            roots.append(sync_id)
            if folder:
                children = walk(data["items"], local_id)
                data = {"name": data["name"], "children": [str(item) for item in children]}
            else:
                data["ssh_jump"]["username"] = None
            records.append(SyncRecord(sync_id, kind, canonical_json(data)))
        return tuple(roots)

    roots = walk(portable["items"], None)
    live_ids = {item.sync_id for item in bindings}
    retired = set(identities.tombstones) | {
        item.sync_id for item in identities.bindings if item.sync_id not in live_ids
    }
    retired = tuple(sorted(retired))
    snapshot = SyncSnapshot(identities.catalog_id, roots, tuple(records), retired)
    return snapshot, SyncIdentities(identities.catalog_id, tuple(bindings), retired)
