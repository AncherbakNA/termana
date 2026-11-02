"""Worker-side remote transaction; GUI applies the returned catalog atomically.

Local edits are guarded again at commit. A failed/uncertain PUT is never blindly
retried. Only portable projections leave the device; encryption is the default.
"""

import base64
import os
from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

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
from termana.profile_sync.wire import encrypted_format, read_plain, write_plain
from termana.ssh.secret_store import SYNC_KEYRING_SERVICE_NAME, KeyringSecretStore


def native_vault():
    import platform

    from termana.ssh.secret_store import _load_platform_backend

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
    previous_recoveries: tuple[str, ...] = field(default=(), repr=False)

    def validate(self):
        if type(self.token) is not str or type(self.recovery) is not str:
            raise SyncError("Invalid sync credentials")
        catalog_id = parse_recovery(self.recovery)[0] if self.recovery else None
        if type(self.previous_recoveries) is not tuple or len(self.previous_recoveries) > 16:
            raise SyncError("Too many previous sync keys; retain your backups before recovery")
        if self.previous_recoveries and catalog_id is None:
            raise SyncError("Previous sync keys require a current key")
        seen = {self.recovery}
        for previous in self.previous_recoveries:
            if parse_recovery(previous)[0] != catalog_id or previous in seen:
                raise SyncError("Invalid previous sync key")
            seen.add(previous)

    def save(self, vault, identity):
        from termana.profile_sync.model import canonical_json

        self.validate()
        document = {"version": 2, "token": self.token, "recovery": self.recovery}
        if self.previous_recoveries:
            document.update(version=3, previous_recoveries=list(self.previous_recoveries))
        vault.set_secret(
            identity,
            canonical_json(document).decode("utf-8"),
        )

    @classmethod
    def load(cls, vault, identity):
        from termana.profile_sync.model import parse_json

        raw = vault.get_secret(identity)
        if raw is None:
            raise SyncError("Sync credentials are unavailable; sign in and enter your recovery key")
        try:
            data = parse_json(raw.encode("utf-8"), limit=65536)
            if (
                set(data)
                != (
                    {"version", "token", "recovery"}
                    | ({"previous_recoveries"} if data.get("version") == 3 else set())
                )
                or type(data["version"]) is not int
                or data["version"] not in (1, 2, 3)
            ):
                raise ValueError()
            if data["version"] == 1 or data["recovery"]:
                parse_recovery(data["recovery"])
            elif type(data["recovery"]) is not str:
                raise ValueError()
            if type(data["token"]) is not str:
                raise ValueError()
            history = data.get("previous_recoveries", [])
            if type(history) is not list or len(history) > 16:
                raise ValueError()
            for value in history:
                parse_recovery(value)
            result = cls(data["token"], data["recovery"], tuple(history))
            result.validate()
            return result
        except (ValueError, TypeError, AttributeError):
            raise SyncError("Saved sync credentials are invalid; reconnect sync") from None


@dataclass(frozen=True)
class SyncTransaction:
    plan: object = field(repr=False)
    state: LocalSyncState = field(repr=False)


def new_binding(catalog, client, secrets, vault, *, recovery=None, encrypted=True):
    """Validate private ownership BEFORE writing any credential or remote data."""
    repository_id, account_id = client.validate()
    existing = client.read()
    if type(encrypted) is not bool:
        raise SyncError("Choose a supported encryption mode")
    remote_encrypted = None if existing is None else encrypted_format(existing[0])
    if remote_encrypted is not None and remote_encrypted != encrypted:
        raise SyncError("Repository encryption differs. Select its current mode to connect first.")
    if remote_encrypted and not recovery:
        raise SyncError(
            "This repository already has a catalog. Enter its recovery key before connecting."
        )
    if not encrypted:
        catalog_id = (
            uuid4() if existing is None else read_plain(existing[0], repository_id).catalog_id
        )
        credentials = SyncSecrets(secrets.token, "")
    elif recovery:
        catalog_id, key = parse_recovery(recovery)
    else:
        catalog_id, key = uuid4(), os.urandom(32)
    if encrypted:
        if existing is not None:
            decrypt_snapshot(existing[0], key, SyncContext(repository_id, catalog_id))
        credentials = SyncSecrets(secrets.token, recovery_code(catalog_id, key))
    ids = project_catalog(catalog, SyncIdentities(catalog_id))[1]
    state = LocalSyncState(
        client.repository, repository_id, account_id, uuid4(), ids, encrypted=encrypted
    )
    # Vault-first: a failed disk/remote operation cannot lose the only key copy.
    credentials.save(vault, state.secret_id)
    return state, credentials


def restore_credentials(state, token, recovery, vault, client, *, encrypted=None):
    """Reauthorize the SAME device without losing sync IDs/base or private grants."""
    client.validate(repository_id=state.repository_id, account_id=state.account_id)
    try:
        previous = SyncSecrets.load(vault, state.secret_id)
    except SyncError:
        previous = None
    desired = state.encrypted if encrypted is None else encrypted
    if type(desired) is not bool:
        raise SyncError("Choose a supported encryption mode")
    if recovery or previous is None or (desired and not previous.recovery):
        if not recovery and desired:
            raise SyncError(
                "Enter your recovery key, then choose Recover to restore this device"
            ) from None
        catalog_id, key = state.identities.catalog_id, None
        if recovery:
            selected_id, key = parse_recovery(recovery)
            if selected_id != catalog_id:
                raise SyncError("Recovery key belongs to a different catalog")
        remote = client.read()
        verified_key = False
        if remote is None:
            if state.base is not None:
                raise SyncError(
                    "Remote catalog disappeared; recovery cannot be confirmed"
                ) from None
        elif encrypted_format(remote[0]):
            if key is None:
                raise SyncError("Repository is encrypted; enter its recovery key")
            decrypt_snapshot(remote[0], key, SyncContext(state.repository_id, catalog_id))
            verified_key = True
        else:
            if (
                previous is not None
                and previous.recovery
                and recovery
                and previous.recovery != recovery
            ):
                raise SyncError("A different key cannot be verified against a plaintext catalog")
            read_plain(remote[0], state.repository_id, catalog_id)
        history = previous.previous_recoveries if previous is not None else ()
        if (
            previous is not None
            and previous.recovery
            and recovery
            and previous.recovery != recovery
        ):
            if not verified_key:
                raise SyncError(
                    "A different key requires verification against an encrypted catalog"
                )
            # A previously retained key may become current again; never duplicate it.
            history = tuple(
                value for value in dict.fromkeys((*history, previous.recovery)) if value != recovery
            )
        previous = SyncSecrets(
            token, recovery or (previous.recovery if previous is not None else ""), history
        )
    result = SyncSecrets(token, previous.recovery, previous.previous_recoveries)
    result.save(vault, state.secret_id)
    return result


def prepare_encryption_key(state, vault):
    """Vault-first key for explicit conversion; never publish or discard an old key."""
    previous = SyncSecrets.load(vault, state.secret_id)
    if previous.recovery:
        catalog_id, _ = parse_recovery(previous.recovery)
        if catalog_id != state.identities.catalog_id:
            raise SyncError("Recovery key belongs to a different catalog")
        return previous.recovery, False
    if state.encrypted:
        raise SyncError("Saved encryption key is missing; recover it before syncing")
    recovery = recovery_code(state.identities.catalog_id, os.urandom(32))
    SyncSecrets(previous.token, recovery, previous.previous_recoveries).save(vault, state.secret_id)
    return recovery, True


def synchronize(catalog, state, secrets, client, cancelled, *, resolution=None, encrypted=None):
    if cancelled.is_set():
        raise SyncError("Synchronization cancelled")
    client.validate(repository_id=state.repository_id, account_id=state.account_id)
    target_mode = state.encrypted if encrypted is None else encrypted
    if type(target_mode) is not bool:
        raise SyncError("Choose a supported encryption mode")
    catalog_id, key = state.identities.catalog_id, None
    if secrets.recovery:
        selected_id, key = parse_recovery(secrets.recovery)
        if selected_id != catalog_id:
            raise SyncError("Recovery key belongs to a different catalog")
    if target_mode and key is None:
        raise SyncError("Save an encryption recovery key before enabling encryption")
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
        remote_mode = encrypted_format(envelope)
        if remote_mode != state.encrypted and (encrypted is None or target_mode != remote_mode):
            raise SyncError(
                "Repository encryption changed on another computer. Select and confirm its mode before syncing."
            )
        if remote_mode:
            if key is None:
                raise SyncError("Repository is encrypted; enter its recovery key")
            remote = decrypt_snapshot(envelope, key, context)
        else:
            remote = read_plain(envelope, state.repository_id, catalog_id)
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
        if target_mode == remote_mode and encode_snapshot(merged) == encode_snapshot(remote):
            plan = prepare_application(catalog, ids, local, SyncMergeResult(merged, ()))
            return SyncTransaction(
                plan,
                replace(
                    state,
                    identities=plan.identities,
                    base=merged,
                    remote_sha=commit,
                    encrypted=target_mode,
                ),
            )
    plan = prepare_application(catalog, ids, local, SyncMergeResult(merged, ()))
    if cancelled.is_set():
        raise SyncError("Synchronization cancelled")
    payload = (
        encrypt_snapshot(merged, key, context)
        if target_mode
        else write_plain(merged, state.repository_id)
    )
    sha = client.write(payload, sha)
    # From this point acknowledge even if cancellation arrived AFTER the PUT:
    # the GUI owner must finish the atomic local commit or retain the old base.
    return SyncTransaction(
        plan,
        replace(
            state, identities=plan.identities, base=merged, remote_sha=sha, encrypted=target_mode
        ),
    )
