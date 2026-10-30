"""Explicit whole-catalog resolution; never resurrect deleted sync identities."""

import hashlib
from uuid import UUID

from termana.profile_sync.model import (
    SyncError,
    SyncRecord,
    SyncSnapshot,
    canonical_json,
    encode_snapshot,
)


class SyncConflictReport(SyncError):
    def __init__(self, base, local, remote):
        super().__init__(
            "Conflicting edits found. Choose a version or cancel; nothing was overwritten."
        )
        self.local, self.remote = local, remote
        self.fingerprint = hashlib.sha256(
            b"\0".join(encode_snapshot(item) for item in (base, local, remote))
        ).hexdigest()

    def resolve(self, selection):
        if selection not in {"local", "remote"}:
            raise SyncError("Choose a conflict resolution explicitly")
        preferred = self.local if selection == "local" else self.remote
        deleted = set(self.local.tombstones) | set(self.remote.tombstones)
        records = {item.item_id: item for item in preferred.records if item.item_id not in deleted}
        visited, resolved = set(), []

        def visit(identity):
            if identity not in records:
                return False
            visited.add(identity)
            row = records[identity]
            if row.kind == "folder":
                data = row.document()
                data["children"] = [value for value in data["children"] if visit(UUID(value))]
                row = SyncRecord(row.item_id, row.kind, canonical_json(data))
            resolved.append(row)
            return True

        roots = tuple(identity for identity in preferred.roots if visit(identity))
        known = deleted | {
            row.item_id for side in (self.local, self.remote) for row in side.records
        }
        return SyncSnapshot(
            preferred.catalog_id, roots, tuple(resolved), tuple(sorted(known - visited))
        )
