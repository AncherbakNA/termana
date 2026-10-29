"""Consistent coordination of SSH credential metadata and keychain secrets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import wraps
from threading import RLock
from typing import Concatenate, Protocol
from uuid import UUID

from termana.credentials.secret_store import (
    MAX_SSH_PASSWORD_CHARACTERS,
    SecretStore,
    SecretStoreError,
)
from termana.models.ssh_account import SshAccount
from termana.models.ssh_credential import (
    MAX_SSH_CREDENTIAL_GENERATION,
    SshCredentialIndex,
    SshCredentialRef,
    SshEndpoint,
    validate_ssh_credential_generation,
)
from termana.storage.credential_index_store import (
    CredentialIndexStore,
    CredentialIndexStoreError,
)


def _locked_repository_operation[**RepositoryParameters, RepositoryResult](
    operation: Callable[
        Concatenate[SshCredentialRepository, RepositoryParameters],
        RepositoryResult,
    ],
) -> Callable[
    Concatenate[SshCredentialRepository, RepositoryParameters],
    RepositoryResult,
]:
    """Serialize one complete public operation across metadata and secret stores."""

    @wraps(operation)
    def locked(
        self: SshCredentialRepository,
        /,
        *args: RepositoryParameters.args,
        **kwargs: RepositoryParameters.kwargs,
    ) -> RepositoryResult:
        if not self._lock.acquire(blocking=False):
            raise SshCredentialRepositoryBusyError(
                "Saved SSH login operations are temporarily busy"
            )
        try:
            return operation(self, *args, **kwargs)
        finally:
            self._lock.release()

    return locked


class CredentialIndexPersistence(Protocol):
    """Injectable persistence edge used by the repository and its tests."""

    def load(self) -> SshCredentialIndex:
        """Return one complete validated metadata snapshot."""

    def save(self, index: SshCredentialIndex) -> None:
        """Atomically persist one complete metadata snapshot."""


class SshCredentialRepositoryError(RuntimeError):
    """Raised when a credential operation cannot complete consistently."""


class SshCredentialRepositoryBusyError(SshCredentialRepositoryError):
    """Raised when another repository transaction owns the cross-store lock."""


class CredentialRemovalOutcome(Enum):
    """Describe a generation-guarded deletion without exposing secret state."""

    REMOVED = "removed"
    GENERATION_CHANGED = "generation_changed"
    MISSING = "missing"


@dataclass(frozen=True, slots=True, eq=False)
class SshCredentialPasswordSnapshot:
    """Bind one confidential password read to its exact non-secret revision."""

    password: str = field(repr=False)
    generation: int

    def __post_init__(self) -> None:
        """Keep manually constructed snapshots as strict as repository results."""
        if not isinstance(self.password, str):
            raise ValueError("SSH credential password snapshot must contain text")
        validate_ssh_credential_generation(self.generation)


class SshCredentialRepository:
    """Keep keychain passwords and non-secret references in safe lockstep."""

    def __init__(
        self,
        index_store: CredentialIndexPersistence,
        secret_store: SecretStore,
    ) -> None:
        """Accept explicit persistence and secret-store dependencies."""
        self._index_store = index_store
        self._secret_store = secret_store
        self._lock = RLock()
        self._index: SshCredentialIndex | None = None
        self._available_by_endpoint: dict[
            SshEndpoint,
            tuple[SshCredentialRef, ...],
        ] = {}

    @_locked_repository_operation
    def accounts(self) -> tuple[SshAccount, ...]:
        """List metadata only: never unlock a vault to populate a selector."""
        return self._load_index().accounts

    @_locked_repository_operation
    def save_account(self, account: SshAccount, password: str | None) -> SshAccount:
        """Explicit account-editor consent; compensate a failed metadata commit.

        None keeps an existing password. A new account requires an explicit
        password (an empty string is allowed). Edits rotate the generation so
        pending authentication reads cannot submit an obsolete revision.
        """
        if type(account) is not SshAccount:
            raise ValueError("Expected an SSH account")
        if password is not None and (
            type(password) is not str or len(password) > MAX_SSH_PASSWORD_CHARACTERS
        ):
            raise ValueError("Invalid SSH account password")
        index = self._load_index()
        old = next((a for a in index.accounts if a.account_id == account.account_id), None)
        if old is None and account.generation != 0:
            raise SshCredentialRepositoryError("The account was removed; create a new account")
        if old is None and password is None:
            raise ValueError("Enter a password for the new account")
        if old is not None and account.generation != old.generation:
            raise SshCredentialRepositoryError("The account changed; reopen its editor")
        account = replace(account, generation=1 if old is None else old.generation + 1)
        aliases = frozenset(c.credential_id for c in index.credentials
                            if c.account_id == account.account_id)
        updated = replace(
            index.without_credentials(aliases),
            accounts=(*(a for a in index.accounts if a.account_id != account.account_id), account),
        )
        try:
            previous = self._secret_store.get_secret(account.account_id)
        except SecretStoreError:
            raise SshCredentialRepositoryError("Could not save the account in the system vault") from None
        if old is None and previous is not None:
            raise SshCredentialRepositoryError("This account ID already has a vault entry")
        if password is not None:
            try:
                self._secret_store.set_secret(account.account_id, password)
            except SecretStoreError:
                self._rollback_secret(account.account_id, previous)
                raise SshCredentialRepositoryError("Could not save the account in the system vault") from None
        try:
            self._save_index(updated)
        except SshCredentialRepositoryError:
            if password is not None:
                self._rollback_secret(account.account_id, previous)
            raise
        self._available_by_endpoint.clear()
        return account

    @_locked_repository_operation
    def remove_account(self, account: SshAccount) -> None:
        """Delete one exact revision and its endpoint projections, not profiles."""
        if type(account) is not SshAccount:
            raise ValueError("Expected an SSH account")
        index = self._load_index()
        old = next((a for a in index.accounts if a.account_id == account.account_id), None)
        if old != account:
            raise SshCredentialRepositoryError("The account changed; refresh the list")
        ids = frozenset(c.credential_id for c in index.credentials
                        if c.account_id == account.account_id)
        updated = replace(index.without_credentials(ids),
                          accounts=tuple(a for a in index.accounts if a != account))
        try:
            previous = self._secret_store.get_secret(account.account_id)
        except SecretStoreError:
            raise SshCredentialRepositoryError("Could not remove the account from the system vault") from None
        try:
            self._secret_store.delete_secret(account.account_id)
        except SecretStoreError:
            self._rollback_secret(account.account_id, previous)
            raise SshCredentialRepositoryError("Could not remove the account from the system vault") from None
        try:
            self._save_index(updated)
        except SshCredentialRepositoryError:
            self._rollback_secret(account.account_id, previous)
            raise
        self._available_by_endpoint.clear()

    @_locked_repository_operation
    def account_reference(self, account_id: UUID, endpoint: SshEndpoint) -> SshCredentialRef:
        """Project an explicitly bound account onto this exact authentication target.

        No secret is read here. Only this endpoint reference is passed into the
        existing trust-before-password and generation-checked auth flow.
        """
        if not isinstance(account_id, UUID) or type(endpoint) is not SshEndpoint:
            raise ValueError("Expected an account ID and SSH endpoint")
        index = self._load_index()
        account = next((a for a in index.accounts if a.account_id == account_id), None)
        if account is None:
            raise SshCredentialRepositoryError("The linked SSH account is unavailable")
        existing = next((c for c in index.credentials
                         if c.account_id == account_id and c.endpoint == endpoint), None)
        if existing is not None:
            return existing
        credential = SshCredentialRef(endpoint, account.username, generation=account.generation,
                                      account_id=account_id)
        self._save_index(index.with_credential(credential))
        return credential

    @classmethod
    def from_stores(
        cls,
        index_store: CredentialIndexStore,
        secret_store: SecretStore,
    ) -> SshCredentialRepository:
        """Name the production composition while retaining protocol-based tests."""
        return cls(index_store=index_store, secret_store=secret_store)

    @_locked_repository_operation
    def load_index(self) -> SshCredentialIndex:
        """Expose a validated non-secret snapshot for presentation code."""
        return self._load_index()

    @_locked_repository_operation
    def saved_credentials(self) -> tuple[SshCredentialRef, ...]:
        """List saved-login metadata without reading or unlocking any password."""
        return tuple(c for c in self._load_index().credentials if c.account_id is None)

    @_locked_repository_operation
    def available_credentials(
        self,
        endpoint: SshEndpoint,
        *,
        prune_stale: bool = True,
    ) -> tuple[SshCredentialRef, ...]:
        """List only references whose keychain passwords still exist."""
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        index = self._load_index()
        return self._available_credentials(
            index,
            endpoint,
            prune_stale=prune_stale,
        )

    @_locked_repository_operation
    def login_references(
        self, endpoint: SshEndpoint, *, profile_id: UUID | None = None
    ) -> tuple[SshCredentialRef, ...]:
        """Order login metadata without unlocking passwords or pruning records.

        Choosing an account is not proof that its password is still available.
        Authentication reads only the chosen ID, endpoint and generation later.
        """
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        if profile_id is not None and not isinstance(profile_id, UUID):
            raise ValueError("SSH profile ID must be a UUID or None")
        index = self._load_index()
        credentials = index.credentials_for(endpoint)
        preferred = (
            index.preferred_for_profile(profile_id, endpoint)
            if profile_id is not None
            else index.preferred_for_endpoint(endpoint)
        )
        if preferred is None or preferred not in credentials:
            return credentials
        return (preferred, *(credential for credential in credentials if credential != preferred))

    @_locked_repository_operation
    def remember_login_reference(
        self,
        endpoint: SshEndpoint,
        credential_id: UUID | None,
        *,
        profile_id: UUID | None = None,
    ) -> None:
        """Store chooser order, not password availability or authentication proof."""
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        if profile_id is not None and not isinstance(profile_id, UUID):
            raise ValueError("SSH profile ID must be a UUID or None")
        index = self._load_index()
        if credential_id is not None:
            credential = index.credential(credential_id)
            if credential is None or credential.endpoint != endpoint:
                raise SshCredentialRepositoryError("The selected SSH login reference changed")
        updated = (
            index.with_profile_preference(profile_id, endpoint, credential_id)
            if profile_id is not None
            else index.with_endpoint_preference(endpoint, credential_id)
        )
        self._save_index(updated)

    @_locked_repository_operation
    def login_credentials(
        self,
        endpoint: SshEndpoint,
        *,
        profile_id: UUID | None = None,
    ) -> tuple[SshCredentialRef, ...]:
        """Return usable login choices, preferred first, from one locked snapshot.

        This is the connect-time composite read.  It keeps availability checks,
        stale-reference pruning, and preference ordering inside one repository
        transaction so the UI never stitches together observations from different
        metadata snapshots.
        """
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        if profile_id is not None and not isinstance(profile_id, UUID):
            raise ValueError("SSH profile ID must be a UUID or None")
        index = self._load_index()
        credentials = self._available_credentials(index, endpoint, prune_stale=True)
        preferred = (
            index.preferred_for_profile(profile_id, endpoint)
            if profile_id is not None
            else index.preferred_for_endpoint(endpoint)
        )
        if preferred is None or preferred not in credentials:
            return credentials
        return (
            preferred,
            *(credential for credential in credentials if credential != preferred),
        )

    def _available_credentials(
        self,
        index: SshCredentialIndex,
        endpoint: SshEndpoint,
        *,
        prune_stale: bool,
    ) -> tuple[SshCredentialRef, ...]:
        """Resolve availability while the caller owns the repository lock."""
        cached = self._available_by_endpoint.get(endpoint)
        if cached is not None:
            return cached
        candidates = index.credentials_for(endpoint)
        if not candidates:
            if prune_stale:
                self._available_by_endpoint[endpoint] = ()
            return ()

        available: list[SshCredentialRef] = []
        stale_ids: set[UUID] = set()
        try:
            for credential in candidates:
                password = self._secret_store.get_secret(credential.credential_id)
                if password is None:
                    stale_ids.add(credential.credential_id)
                else:
                    available.append(credential)
        except SecretStoreError:
            raise SshCredentialRepositoryError(
                "Saved SSH logins are temporarily unavailable"
            ) from None

        if prune_stale and stale_ids:
            self._save_index(index.without_credentials(frozenset(stale_ids)))
        result = tuple(available)
        if prune_stale:
            self._available_by_endpoint[endpoint] = result
        return result

    @_locked_repository_operation
    def preferred_for_profile(
        self,
        profile_id: UUID,
        endpoint: SshEndpoint,
    ) -> SshCredentialRef | None:
        """Resolve the usable login placed first for this profile and endpoint."""
        index = self._load_index()
        credential = index.preferred_for_profile(profile_id, endpoint)
        return self._usable_preference(index, credential)

    @_locked_repository_operation
    def preferred_for_endpoint(self, endpoint: SshEndpoint) -> SshCredentialRef | None:
        """Resolve the usable login placed first for this Quick Connect endpoint."""
        index = self._load_index()
        credential = index.preferred_for_endpoint(endpoint)
        return self._usable_preference(index, credential)

    @_locked_repository_operation
    def password_for(self, credential_id: UUID, endpoint: SshEndpoint) -> str | None:
        """Read without metadata writes, after rechecking the exact endpoint binding."""
        snapshot = self._password_for_with_generation(credential_id, endpoint)
        return None if snapshot is None else snapshot.password

    @_locked_repository_operation
    def password_for_with_generation(
        self,
        credential_id: UUID,
        endpoint: SshEndpoint,
    ) -> SshCredentialPasswordSnapshot | None:
        """Read one password/revision without committing metadata under the same lock.

        Authentication workers can finish after their UI owner is gone. Missing
        secrets invalidate only the local availability cache here; stale-reference
        pruning belongs to lifecycle-owned availability/preference operations.
        """
        return self._password_for_with_generation(credential_id, endpoint)

    def _password_for_with_generation(
        self,
        credential_id: UUID,
        endpoint: SshEndpoint,
    ) -> SshCredentialPasswordSnapshot | None:
        """Implement the locked secret/revision snapshot shared by both read APIs."""
        if not isinstance(credential_id, UUID):
            raise ValueError("SSH credential ID must be a UUID")
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        index = self._load_index()
        credential = index.credential(credential_id)
        if credential is None or credential.endpoint != endpoint:
            return None
        try:
            password = self._secret_store.get_secret(credential.account_id or credential_id)
        except SecretStoreError:
            raise SshCredentialRepositoryError(
                "The saved SSH password is temporarily unavailable"
            ) from None
        if password is None:
            self._available_by_endpoint.pop(endpoint, None)
            return None
        return SshCredentialPasswordSnapshot(
            password=password,
            generation=credential.generation,
        )

    @_locked_repository_operation
    def save_password(
        self,
        endpoint: SshEndpoint,
        username: str,
        password: str,
    ) -> SshCredentialRef:
        """Save a consented password, rolling back if metadata cannot commit."""
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        index = self._load_index()
        credential = next(
            (item for item in index.credentials_for(endpoint) if item.username == username),
            None,
        )
        if credential is None:
            credential = SshCredentialRef(endpoint=endpoint, username=username)
            updated_index = index.with_credential(credential)
            previous_password = None
        else:
            if credential.generation >= MAX_SSH_CREDENTIAL_GENERATION:
                raise SshCredentialRepositoryError(
                    "The SSH password revision limit has been reached"
                )
            try:
                previous_password = self._secret_store.get_secret(credential.credential_id)
            except SecretStoreError:
                raise SshCredentialRepositoryError(
                    "The SSH password could not be saved in the system credential store"
                ) from None
            credential = SshCredentialRef(
                endpoint=credential.endpoint,
                username=credential.username,
                credential_id=credential.credential_id,
                generation=credential.generation + 1,
            )
            updated_index = index.with_credential(credential)
        try:
            self._secret_store.set_secret(credential.credential_id, password)
        except SecretStoreError:
            self._rollback_secret(credential.credential_id, previous_password)
            raise SshCredentialRepositoryError(
                "The SSH password could not be saved in the system credential store"
            ) from None

        try:
            self._save_index(updated_index)
        except SshCredentialRepositoryError:
            self._rollback_secret(credential.credential_id, previous_password)
            raise
        self._available_by_endpoint.pop(endpoint, None)
        return credential

    @_locked_repository_operation
    def set_profile_preference(
        self,
        profile_id: UUID,
        endpoint: SshEndpoint,
        credential_id: UUID | None,
    ) -> None:
        """Remember or clear a saved-profile chooser preference atomically."""
        index = self._load_index()
        if credential_id is not None:
            self._require_available_credential(index, credential_id, endpoint)
        self._save_index(index.with_profile_preference(profile_id, endpoint, credential_id))

    @_locked_repository_operation
    def set_endpoint_preference(
        self,
        endpoint: SshEndpoint,
        credential_id: UUID | None,
    ) -> None:
        """Remember or clear an exact-endpoint Quick Connect chooser preference."""
        index = self._load_index()
        if credential_id is not None:
            self._require_available_credential(index, credential_id, endpoint)
        self._save_index(index.with_endpoint_preference(endpoint, credential_id))

    @_locked_repository_operation
    def remove_credential(self, credential_id: UUID) -> bool:
        """Delete one password and reference through the atomic batch operation."""
        if not isinstance(credential_id, UUID):
            raise ValueError("SSH credential ID must be a UUID")
        return bool(self.remove_credentials(frozenset({credential_id})))

    @_locked_repository_operation
    def remove_credential_if_generation_matches(
        self,
        credential_id: UUID,
        expected_generation: int,
    ) -> CredentialRemovalOutcome:
        """Remove only the exact credential revision observed by the caller."""
        if not isinstance(credential_id, UUID):
            raise ValueError("SSH credential ID must be a UUID")
        validate_ssh_credential_generation(expected_generation)
        credential = self._load_index().credential(credential_id)
        if credential is None:
            return CredentialRemovalOutcome.MISSING
        if credential.generation != expected_generation:
            return CredentialRemovalOutcome.GENERATION_CHANGED
        removed = self.remove_credentials(frozenset({credential_id}))
        return CredentialRemovalOutcome.REMOVED if removed else CredentialRemovalOutcome.MISSING

    @_locked_repository_operation
    def remove_credentials(
        self,
        credential_ids: frozenset[UUID],
    ) -> tuple[SshCredentialRef, ...]:
        """Delete saved logins in one compensated metadata transaction.

        Every existing password is read before the first mutation so a locked or
        unavailable keychain cannot cause a partial batch. Password values remain
        inside this repository and are retained only for the rollback window.
        """
        if not isinstance(credential_ids, frozenset) or any(
            not isinstance(item, UUID) for item in credential_ids
        ):
            raise ValueError("SSH credential IDs must be a frozenset of UUIDs")
        index = self._load_index()
        credentials = tuple(
            credential
            for credential in index.credentials
            if credential.credential_id in credential_ids
        )
        if not credentials:
            return ()

        previous_passwords: dict[UUID, str | None] = {}
        try:
            for credential in credentials:
                previous_passwords[credential.credential_id] = self._secret_store.get_secret(
                    credential.credential_id
                )
        except SecretStoreError:
            previous_passwords.clear()
            raise SshCredentialRepositoryError(
                "Saved SSH passwords could not be removed from the system credential store"
            ) from None

        attempted_deletions: list[UUID] = []
        affected_endpoints = frozenset(credential.endpoint for credential in credentials)
        try:
            for credential in credentials:
                credential_id = credential.credential_id
                if previous_passwords[credential_id] is None:
                    continue
                # Record before calling the backend: a backend may delete the item
                # successfully and still report a late failure.
                attempted_deletions.append(credential_id)
                self._secret_store.delete_secret(credential_id)
        except SecretStoreError:
            self._invalidate_available_credentials(affected_endpoints)
            try:
                self._restore_removed_secrets(
                    attempted_deletions,
                    previous_passwords,
                )
            finally:
                previous_passwords.clear()
            raise SshCredentialRepositoryError(
                "Saved SSH passwords could not be removed from the system credential store"
            ) from None

        try:
            self._save_index(index.without_credentials(credential_ids))
        except SshCredentialRepositoryError:
            self._invalidate_available_credentials(affected_endpoints)
            self._restore_removed_secrets(
                attempted_deletions,
                previous_passwords,
            )
            raise
        finally:
            previous_passwords.clear()

        self._invalidate_available_credentials(affected_endpoints)
        return credentials

    @_locked_repository_operation
    def remove_all_credentials(self) -> tuple[SshCredentialRef, ...]:
        """Delete every indexed saved SSH login as one compensated batch."""
        credential_ids = frozenset(
            credential.credential_id for credential in self._load_index().credentials
        )
        return self.remove_credentials(credential_ids)

    def _restore_removed_secrets(
        self,
        attempted_deletions: list[UUID],
        previous_passwords: dict[UUID, str | None],
    ) -> None:
        """Best-effort restore every attempted deletion before reporting failure."""
        rollback_failed = False
        for credential_id in attempted_deletions:
            previous_password = previous_passwords[credential_id]
            if previous_password is None:
                continue
            try:
                self._secret_store.set_secret(credential_id, previous_password)
            except SecretStoreError:
                rollback_failed = True
        if rollback_failed:
            raise SshCredentialRepositoryError(
                "Saved SSH login removal could not be completed consistently"
            ) from None

    def _invalidate_available_credentials(
        self,
        endpoints: frozenset[SshEndpoint],
    ) -> None:
        """Forget availability results for every endpoint touched by a batch."""
        for endpoint in endpoints:
            self._available_by_endpoint.pop(endpoint, None)

    def _usable_preference(
        self,
        index: SshCredentialIndex,
        credential: SshCredentialRef | None,
    ) -> SshCredentialRef | None:
        """Hide and safely prune a preference whose keychain item disappeared."""
        if credential is None:
            return None
        cached = self._available_by_endpoint.get(credential.endpoint)
        if cached is not None:
            return credential if credential in cached else None
        try:
            password = self._secret_store.get_secret(credential.credential_id)
        except SecretStoreError:
            raise SshCredentialRepositoryError(
                "Saved SSH logins are temporarily unavailable"
            ) from None
        if password is not None:
            return credential
        self._save_index(index.without_credentials(frozenset({credential.credential_id})))
        self._available_by_endpoint.pop(credential.endpoint, None)
        return None

    def _require_available_credential(
        self,
        index: SshCredentialIndex,
        credential_id: UUID,
        endpoint: SshEndpoint,
    ) -> None:
        """Reject dangling and cross-endpoint preferences before persistence."""
        credential = index.credential(credential_id)
        if credential is None:
            raise SshCredentialRepositoryError("The selected SSH login no longer exists")
        if credential.endpoint != endpoint:
            raise SshCredentialRepositoryError(
                "The selected SSH login belongs to a different endpoint"
            )
        cached = self._available_by_endpoint.get(endpoint)
        if cached is not None:
            if credential in cached:
                return
            raise SshCredentialRepositoryError(
                "The selected SSH login no longer has a saved password"
            )
        try:
            password = self._secret_store.get_secret(credential_id)
        except SecretStoreError:
            raise SshCredentialRepositoryError(
                "Saved SSH logins are temporarily unavailable"
            ) from None
        if password is None:
            raise SshCredentialRepositoryError(
                "The selected SSH login no longer has a saved password"
            )

    def _rollback_secret(self, credential_id: UUID, previous_password: str | None) -> None:
        """Restore the keychain side after a failed metadata commit."""
        try:
            if previous_password is None:
                self._secret_store.delete_secret(credential_id)
            else:
                self._secret_store.set_secret(credential_id, previous_password)
        except SecretStoreError:
            raise SshCredentialRepositoryError(
                "Credential metadata could not be saved and keychain rollback failed"
            ) from None

    def _load_index(self) -> SshCredentialIndex:
        """Translate persistence errors without ever handling password values."""
        if self._index is not None:
            return self._index
        try:
            index = self._index_store.load()
        except CredentialIndexStoreError as error:
            raise SshCredentialRepositoryError(str(error)) from error
        self._index = index
        return index

    def _save_index(self, index: SshCredentialIndex) -> None:
        """Commit one complete metadata snapshot or raise an application error."""
        try:
            self._index_store.save(index)
        except CredentialIndexStoreError as error:
            raise SshCredentialRepositoryError(str(error)) from error
        self._index = index
