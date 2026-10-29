"""Prepare, but NEVER persist, a sync catalog with safe local identity bindings."""

from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

from termana.models.connection import ConnectionCatalog, ConnectionFolder, ConnectionProtocol
from termana.profile_sync.merge import SyncMergeResult
from termana.profile_sync.model import (
    SyncBinding,
    SyncError,
    SyncIdentities,
    SyncSnapshot,
    canonical_json,
    encode_snapshot,
    project_catalog,
)
from termana.storage.connection_transfer import decode_connection_transfer


@dataclass(frozen=True, slots=True)
class PreparedSyncApplication:
    source_catalog: ConnectionCatalog = field(repr=False)
    source_identities: SyncIdentities = field(repr=False)
    catalog: ConnectionCatalog = field(repr=False)
    identities: SyncIdentities = field(repr=False)
    detached_local_ids: tuple = ()

    def check_current(self, catalog, identities):
        """A future atomic commit must call this under its exclusive writer lock."""
        if catalog != self.source_catalog or identities != self.source_identities:
            raise SyncError("Local connections changed; prepare synchronization again")


def _boundary(profile):
    return (
        profile.protocol,
        profile.host,
        profile.port,
        replace(profile.ssh_jump, username=None),
        profile.ssh_algorithms,
        profile.ssh_cipher,
        profile.rdp_options.admin_session,
    )


def prepare_application(
    local, identities, expected_local: SyncSnapshot, result: SyncMergeResult
) -> PreparedSyncApplication:
    """Require a conflict-free merge, retain native IDs only across safe bindings.

    Endpoint/route/crypto/admin changes get a NEW local profile ID with no private
    auth/clipboard/tunnel settings. Old vault items are not deleted or accessed.
    No live backend, GUI, file, keyring or trust state is touched here.
    """
    if (
        type(local) is not ConnectionCatalog
        or type(identities) is not SyncIdentities
        or type(expected_local) is not SyncSnapshot
        or type(result) is not SyncMergeResult
        or result.snapshot is None
        or result.conflicts
    ):
        raise SyncError("A current catalog and conflict-free sync result are required")
    projected, _ = project_catalog(local, identities)
    if encode_snapshot(projected) != encode_snapshot(expected_local):
        raise SyncError("Local connections changed; merge again before applying")
    incoming = result.snapshot
    if incoming.catalog_id != identities.catalog_id:
        raise SyncError("Sync result belongs to another catalog")
    records = {item.item_id: item for item in incoming.records}
    known = {item.item_id for item in projected.records} | set(projected.tombstones)
    if not known <= set(records) | set(incoming.tombstones) or not set(projected.tombstones) <= set(
        incoming.tombstones
    ):
        raise SyncError("Sync result lost known identity history")
    by_sync = {item.sync_id: item for item in identities.bindings}

    def tree(item_id):
        record = records[item_id]
        data = record.document()
        if record.kind == "connection":
            return data
        return {
            "type": "folder",
            "name": data["name"],
            "items": [tree(UUID(item)) for item in data["children"]],
        }

    decoded = decode_connection_transfer(
        canonical_json(
            {
                "format": "termana-connections",
                "version": 12,
                "items": [tree(item) for item in incoming.roots],
            }
        )
    ).catalog
    forbidden = (
        set(records)
        | set(incoming.tombstones)
        | {identities.catalog_id}
        | {item.profile_id for item in local.profiles}
        | {item.folder_id for item in local.folders}
    )
    profiles, folders, bindings, detached = [], [], [], []

    def fresh():
        for _attempt in range(128):
            candidate = uuid4()
            if type(candidate) is UUID and candidate.int and candidate not in forbidden:
                forbidden.add(candidate)
                return candidate
        raise SyncError("Could not allocate a fresh local sync identity")

    def walk(sync_ids, parent):
        native_ids = []
        for sync_id, portable in zip(sync_ids, decoded.children_of(parent), strict=True):
            record = records[sync_id]
            binding = by_sync.get(sync_id)
            if binding is not None and binding.kind != record.kind:
                raise SyncError("A sync identity cannot change record kind")
            old = (
                None
                if binding is None
                else (
                    local.folder_by_id(binding.local_id)
                    if record.kind == "folder"
                    else local.profile_by_id(binding.local_id)
                )
            )
            local_id = (
                old.folder_id
                if isinstance(old, ConnectionFolder)
                else (old.profile_id if old is not None else fresh())
            )
            if record.kind == "folder":
                bindings.append(SyncBinding(local_id, sync_id, record.kind))
                child_ids = tuple(UUID(item) for item in record.document()["children"])
                children = walk(child_ids, portable.folder_id)
                folders.append(replace(portable, folder_id=local_id, child_ids=children))
            else:
                preserve = old is not None and _boundary(old) == _boundary(portable)
                if old is not None and not preserve:
                    detached.append(local_id)
                    local_id = fresh()
                profile = replace(portable, profile_id=local_id)
                if preserve:
                    profile = replace(
                        profile,
                        username=old.username,
                        ssh_account_id=old.ssh_account_id,
                        ssh_identity=old.ssh_identity,
                        ssh_tunnels=old.ssh_tunnels,
                        ssh_jump=replace(profile.ssh_jump, username=old.ssh_jump.username),
                    )
                    if profile.protocol is ConnectionProtocol.RDP:
                        profile = replace(
                            profile,
                            rdp_options=replace(
                                profile.rdp_options,
                                username=old.rdp_options.username,
                                domain=old.rdp_options.domain,
                                clipboard_to_local=old.rdp_options.clipboard_to_local,
                                clipboard_to_remote=old.rdp_options.clipboard_to_remote,
                            ),
                        )
                profiles.append(profile)
                bindings.append(SyncBinding(local_id, sync_id, record.kind))
            native_ids.append(local_id)
        return tuple(native_ids)

    roots = walk(incoming.roots, None)
    # Native storage arrays are not presentation order (roots/child_ids are).
    # Retain their existing order so a no-op pull is an exact local snapshot,
    # not a spurious change just because traversal visited a folder's children.
    profile_order = {item.profile_id: index for index, item in enumerate(local.profiles)}
    folder_order = {item.folder_id: index for index, item in enumerate(local.folders)}
    profiles.sort(key=lambda item: profile_order.get(item.profile_id, len(profile_order)))
    folders.sort(key=lambda item: folder_order.get(item.folder_id, len(folder_order)))
    candidate = ConnectionCatalog(
        root_item_ids=roots, folders=tuple(folders), profiles=tuple(profiles)
    )
    next_ids = SyncIdentities(identities.catalog_id, tuple(bindings), incoming.tombstones)
    return PreparedSyncApplication(local, identities, candidate, next_ids, tuple(detached))
