"""Local-only sync metadata, stored atomically WITH the native catalog.

Contains no token, encryption key or remote permissions. Never upload this
document: native IDs and the merge base are device-owned bookkeeping.
"""

import re
from dataclasses import dataclass, field, replace
from uuid import UUID

from termana.profile_sync.model import (
    SyncBinding,
    SyncError,
    SyncIdentities,
    SyncSnapshot,
    canonical_json,
    decode_snapshot,
    encode_snapshot,
    parse_json,
    project_catalog,
)
from termana.updates.manifest import ReleaseRepository


@dataclass(frozen=True, slots=True)
class LocalSyncState:
    repository: ReleaseRepository
    repository_id: int
    account_id: int
    secret_id: UUID
    identities: SyncIdentities = field(repr=False)
    base: SyncSnapshot | None = field(default=None, repr=False)
    remote_sha: str | None = None

    def __post_init__(self):
        if (
            type(self.repository) is not ReleaseRepository
            or type(self.identities) is not SyncIdentities
            or type(self.secret_id) is not UUID
            or not self.secret_id.int
            or any(
                type(value) is not int or not 1 <= value <= 2**53 - 1
                for value in (self.repository_id, self.account_id)
            )
        ):
            raise SyncError("Invalid local sync binding")
        if (self.base is None) != (self.remote_sha is None):
            raise SyncError("Sync merge base and remote revision must be recorded together")
        if self.base is not None and (
            type(self.base) is not SyncSnapshot
            or self.base.catalog_id != self.identities.catalog_id
            or type(self.remote_sha) is not str
            or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", self.remote_sha) is None
        ):
            raise SyncError("Invalid local sync base or remote revision")
        if self.base is not None:
            # Snapshot wire encoding has canonical record order. Store the same
            # representation in memory so a save/reload preserves state equality.
            object.__setattr__(self, "base", decode_snapshot(encode_snapshot(self.base)))

    def reconciled(self, catalog):
        """Track native edits/new IDs/deletions without changing the remote base."""
        _, identities = project_catalog(catalog, self.identities)
        return replace(self, identities=identities)

    def validate_catalog(self, catalog):
        if self.reconciled(catalog).identities != self.identities:
            raise SyncError("Local sync map does not match the saved catalog")

    def document(self):
        return {
            "version": 1,
            "repository": self.repository.identity,
            "repository_id": self.repository_id,
            "account_id": self.account_id,
            "secret_id": str(self.secret_id),
            "catalog_id": str(self.identities.catalog_id),
            "bindings": [
                {"local_id": str(item.local_id), "sync_id": str(item.sync_id), "kind": item.kind}
                for item in self.identities.bindings
            ],
            "tombstones": [str(item) for item in self.identities.tombstones],
            "base": None if self.base is None else parse_json(encode_snapshot(self.base)),
            "remote_sha": self.remote_sha,
        }

    @classmethod
    def from_document(cls, data):
        try:
            if (
                type(data) is not dict
                or set(data)
                != {
                    "version",
                    "repository",
                    "repository_id",
                    "account_id",
                    "secret_id",
                    "catalog_id",
                    "bindings",
                    "tombstones",
                    "base",
                    "remote_sha",
                }
                or type(data["version"]) is not int
                or data["version"] != 1
                or type(data["bindings"]) is not list
                or len(data["bindings"]) > 10000
                or type(data["tombstones"]) is not list
                or len(data["tombstones"]) > 50000
            ):
                raise SyncError("Invalid local sync state")

            def uuid(value):
                if type(value) is not str:
                    raise SyncError("Invalid local sync identity")
                result = UUID(value)
                if str(result) != value or not result.int:
                    raise SyncError("Invalid local sync identity")
                return result

            bindings = []
            for row in data["bindings"]:
                if type(row) is not dict or set(row) != {"local_id", "sync_id", "kind"}:
                    raise SyncError("Invalid local sync map")
                bindings.append(
                    SyncBinding(uuid(row["local_id"]), uuid(row["sync_id"]), row["kind"])
                )
            identities = SyncIdentities(
                uuid(data["catalog_id"]),
                tuple(bindings),
                tuple(uuid(item) for item in data["tombstones"]),
            )
            return cls(
                ReleaseRepository.parse(data["repository"]),
                data["repository_id"],
                data["account_id"],
                uuid(data["secret_id"]),
                identities,
                None if data["base"] is None else decode_snapshot(canonical_json(data["base"])),
                data["remote_sha"],
            )
        except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
            raise SyncError("Invalid local sync state") from None
