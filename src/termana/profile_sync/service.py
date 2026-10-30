"""Worker-side remote transaction; GUI applies the returned catalog atomically.

Local edits are guarded again at commit. A failed/uncertain PUT is never blindly
retried. Only portable encrypted projections leave the device.
"""

import base64
import os
from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

from termana.credentials.secret_store import SYNC_KEYRING_SERVICE_NAME, KeyringSecretStore
from termana.profile_sync.application import prepare_application
from termana.profile_sync.conflicts import SyncConflictReport
from termana.profile_sync.crypto import SyncContext, decrypt_snapshot, encrypt_snapshot
from termana.profile_sync.local_state import LocalSyncState
from termana.profile_sync.merge import SyncMergeResult, merge_snapshots
from termana.profile_sync.model import (
    SyncError,
    SyncIdentities,
    SyncSnapshot,
    encode_snapshot,
    project_catalog,
)


def native_vault():
    import platform

    from termana.credentials.secret_store import _load_platform_backend

    def backend():
        selected = _load_platform_backend()
        if platform.system() == "Linux":
            from termana.rdp.linux_vault import existing_collection_backend

            selected = existing_collection_backend(selected, purpose="sync")
        return selected

    return KeyringSecretStore(service_name=SYNC_KEYRING_SERVICE_NAME, backend_factory=backend)


def recovery_code(catalog_id, key):
    if type(catalog_id) is not UUID or type(key) is not bytes or len(key) != 32:
        raise SyncError("Invalid recovery key")
    return (
        "termana-sync-v1:" + str(catalog_id) + ":" + base64.urlsafe_b64encode(key).decode("ascii")
    )


def parse_recovery(value):
    try:
        prefix, identity, encoded = value.split(":")
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
        catalog_id = UUID(identity)
        if prefix != "termana-sync-v1" or recovery_code(catalog_id, key) != value:
            raise ValueError()
        return catalog_id, key
    except (ValueError, TypeError, AttributeError):
        raise SyncError("Enter the complete Termana sync recovery key") from None


@dataclass(frozen=True)
class SyncSecrets:
    token: str = field(repr=False)
    recovery: str = field(repr=False)

    def save(self, vault, identity):
        from termana.profile_sync.model import canonical_json

        parse_recovery(self.recovery)
        vault.set_secret(
            identity,
            canonical_json({"version": 1, "token": self.token, "recovery": self.recovery}).decode(
                "utf-8"
            ),
        )

    @classmethod
    def load(cls, vault, identity):
        from termana.profile_sync.model import parse_json

        raw = vault.get_secret(identity)
        if raw is None:
            raise SyncError("Sync credentials are unavailable; sign in and enter your recovery key")
        try:
            data = parse_json(raw.encode("utf-8"), limit=65536)
            if set(data) != {"version", "token", "recovery"} or data["version"] != 1:
                raise ValueError()
            parse_recovery(data["recovery"])
            if type(data["token"]) is not str:
                raise ValueError()
            return cls(data["token"], data["recovery"])
        except (ValueError, TypeError, AttributeError):
            raise SyncError("Saved sync credentials are invalid; reconnect sync") from None


@dataclass(frozen=True)
class SyncTransaction:
    plan: object = field(repr=False)
    state: LocalSyncState = field(repr=False)


def new_binding(catalog, client, secrets, vault, *, recovery=None):
    """Validate private ownership BEFORE writing any credential or remote data."""
    repository_id, account_id = client.validate()
    existing = client.read()
    if existing is not None and not recovery:
        raise SyncError(
            "This repository already has a catalog. Enter its recovery key before connecting."
        )
    if recovery:
        catalog_id, key = parse_recovery(recovery)
    else:
        catalog_id, key = uuid4(), os.urandom(32)
    if existing is not None:
        decrypt_snapshot(existing[0], key, SyncContext(repository_id, catalog_id))
    ids = project_catalog(catalog, SyncIdentities(catalog_id))[1]
    state = LocalSyncState(client.repository, repository_id, account_id, uuid4(), ids)
    credentials = SyncSecrets(secrets.token, recovery_code(catalog_id, key))
    # Vault-first: a failed disk/remote operation cannot lose the only key copy.
    credentials.save(vault, state.secret_id)
    return state, credentials


def restore_credentials(state, token, recovery, vault, client):
    """Reauthorize the SAME device without losing sync IDs/base or private grants."""
    client.validate(repository_id=state.repository_id, account_id=state.account_id)
    try:
        previous = SyncSecrets.load(vault, state.secret_id)
    except SyncError:
        if not recovery:
            raise SyncError(
                "Enter your recovery key, then choose Recover to restore this device"
            ) from None
        catalog_id, key = parse_recovery(recovery)
        if catalog_id != state.identities.catalog_id:
            raise SyncError("Recovery key belongs to a different catalog") from None
        remote = client.read()
        if remote is None:
            if state.base is not None:
                raise SyncError(
                    "Remote catalog disappeared; recovery cannot be confirmed"
                ) from None
        else:
            decrypt_snapshot(remote[0], key, SyncContext(state.repository_id, catalog_id))
        previous = SyncSecrets(token, recovery)
    result = SyncSecrets(token, previous.recovery)
    result.save(vault, state.secret_id)
    return result


def synchronize(catalog, state, secrets, client, cancelled, *, resolution=None):
    if cancelled.is_set():
        raise SyncError("Synchronization cancelled")
    client.validate(repository_id=state.repository_id, account_id=state.account_id)
    catalog_id, key = parse_recovery(secrets.recovery)
    if catalog_id != state.identities.catalog_id:
        raise SyncError("Recovery key belongs to a different catalog")
    context = SyncContext(state.repository_id, catalog_id)
    local, ids = project_catalog(catalog, state.identities)
    previous = client.read()
    if cancelled.is_set():
        raise SyncError("Synchronization cancelled")
    if previous is None:
        if state.base is not None:
            raise SyncError("Remote catalog disappeared; refusing to recreate or overwrite it")
        merged, sha = local, None
    else:
        envelope, sha, commit = previous
        client.check_history(state.remote_sha, commit)
        remote = decrypt_snapshot(envelope, key, context)
        if state.base is None:
            # First device import: keep existing local profiles, append them to
            # the downloaded tree. IDs are independently allocated per device.
            existing = {row.item_id: row for row in remote.records}
            if any(
                row.item_id in existing and existing[row.item_id] != row for row in local.records
            ):
                raise SyncError(
                    "First-sync history differs; resolve edits before reconnecting sync"
                )
            merged = SyncSnapshot(
                catalog_id,
                remote.roots + tuple(row for row in local.roots if row not in remote.roots),
                remote.records + tuple(row for row in local.records if row.item_id not in existing),
                tuple(set(remote.tombstones) | set(local.tombstones)),
            )
        else:
            result = merge_snapshots(state.base, local, remote)
            if result.conflicts:
                report = SyncConflictReport(state.base, local, remote)
                if resolution is None or resolution[0] != report.fingerprint:
                    raise report
                merged = report.resolve(resolution[1])
            else:
                merged = result.snapshot
        if encode_snapshot(merged) == encode_snapshot(remote):
            plan = prepare_application(catalog, ids, local, SyncMergeResult(merged, ()))
            return SyncTransaction(
                plan, replace(state, identities=plan.identities, base=merged, remote_sha=commit)
            )
    plan = prepare_application(catalog, ids, local, SyncMergeResult(merged, ()))
    if cancelled.is_set():
        raise SyncError("Synchronization cancelled")
    sha = client.write(encrypt_snapshot(merged, key, context), sha)
    # From this point acknowledge even if cancellation arrived AFTER the PUT:
    # the GUI owner must finish the atomic local commit or retain the old base.
    return SyncTransaction(
        plan, replace(state, identities=plan.identities, base=merged, remote_sha=sha)
    )
