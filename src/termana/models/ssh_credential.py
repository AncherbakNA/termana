"""Non-secret metadata for saved SSH credentials."""

from __future__ import annotations

import ipaddress
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from termana.models.connection import (
    MAX_CONNECTION_HOST_CHARACTERS,
    MAX_CONNECTION_USERNAME_CHARACTERS,
    MAX_NETWORK_PORT,
    MIN_NETWORK_PORT,
)

if TYPE_CHECKING:
    from termana.models.ssh_account import SshAccount

MAX_SSH_CREDENTIALS: Final = 2_048
MAX_SSH_CREDENTIAL_PREFERENCES: Final = 10_000
MAX_SSH_USERNAME_UTF8_BYTES: Final = MAX_CONNECTION_USERNAME_CHARACTERS * 4
MAX_SSH_CREDENTIAL_GENERATION: Final = (1 << 63) - 1
_DIRECTIONAL_FORMATS: Final = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)


def _validated_ssh_host_identity(host: str) -> str:
    """Validate and retain the exact host spelling passed to OpenSSH."""
    if not isinstance(host, str):
        raise ValueError("SSH credential host must be a string")

    if host.startswith("[") or host.endswith("]"):
        raise ValueError("SSH credential host must not contain IPv6 brackets")

    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError as error:
            raise ValueError("SSH credential host must be a valid IPv6 address") from error

    _validate_identity_text(
        host,
        field_name="SSH credential host",
        maximum_length=MAX_CONNECTION_HOST_CHARACTERS,
    )
    if host.startswith("-"):
        raise ValueError("SSH credential host cannot begin with a hyphen")
    if "@" in host:
        raise ValueError("SSH credential host cannot contain @")
    return host


def validate_ssh_username(username: object) -> str:
    """Return one exact display-safe SSH username or raise ``ValueError``.

    Account names are case-sensitive protocol identities, so this function never
    strips or normalizes input. The explicit UTF-8 limit keeps future UI and IPC
    boundaries bounded even when every accepted character uses four bytes.
    """
    if not isinstance(username, str):
        raise ValueError("SSH credential username must be a string")
    if not username:
        raise ValueError("Username cannot be empty")
    if len(username) > MAX_CONNECTION_USERNAME_CHARACTERS:
        raise ValueError("Username is too long")
    if any(character.isspace() for character in username):
        raise ValueError("Username cannot contain whitespace")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.bidirectional(character) in _DIRECTIONAL_FORMATS
        for character in username
    ):
        raise ValueError("Username contains unsafe control or formatting characters")
    try:
        encoded = username.encode("utf-8")
    except UnicodeError:
        raise ValueError("Username contains invalid Unicode text") from None
    if len(encoded) > MAX_SSH_USERNAME_UTF8_BYTES:
        raise ValueError("Username is too long when encoded")
    return username


def _validated_ssh_username_identity(username: str) -> str:
    """Validate and retain the exact case-sensitive remote account name."""
    return validate_ssh_username(username)


def validate_ssh_credential_generation(generation: object) -> int:
    """Return one bounded revision counter or reject loose numeric values."""
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise ValueError("SSH credential generation must be an integer")
    if not 0 <= generation <= MAX_SSH_CREDENTIAL_GENERATION:
        raise ValueError(
            f"SSH credential generation must be between 0 and {MAX_SSH_CREDENTIAL_GENERATION}"
        )
    return generation


def _validate_identity_text(value: str, *, field_name: str, maximum_length: int) -> None:
    """Reject empty, ambiguous, or terminal-active endpoint metadata."""
    if not value:
        raise ValueError(f"{field_name} cannot be empty")
    if len(value) > maximum_length:
        raise ValueError(f"{field_name} is too long")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name} cannot contain whitespace")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.bidirectional(character) in _DIRECTIONAL_FORMATS
        for character in value
    ):
        raise ValueError(f"{field_name} cannot contain control or formatting characters")


@dataclass(frozen=True, slots=True, init=False)
class SshEndpoint:
    """An exact SSH target identity used only for credential lookup."""

    host: str
    port: int

    def __init__(self, host: str, port: int) -> None:
        object.__setattr__(self, "host", _validated_ssh_host_identity(host))
        object.__setattr__(self, "port", port)
        self.__post_init__()

    def __post_init__(self) -> None:
        """Reject non-integer and out-of-range ports."""
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("SSH credential port must be an integer")
        if not MIN_NETWORK_PORT <= self.port <= MAX_NETWORK_PORT:
            raise ValueError(
                f"SSH credential port must be between {MIN_NETWORK_PORT} and {MAX_NETWORK_PORT}"
            )


@dataclass(frozen=True, slots=True, init=False)
class SshCredentialRef:
    """A safe reference to one password held outside application data files."""

    endpoint: SshEndpoint
    username: str
    credential_id: UUID = field(default_factory=uuid4)
    generation: int = 1
    account_id: UUID | None = None

    def __init__(
        self,
        endpoint: SshEndpoint,
        username: str,
        credential_id: UUID | None = None,
        generation: int = 1,
        account_id: UUID | None = None,
    ) -> None:
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "username", _validated_ssh_username_identity(username))
        object.__setattr__(
            self,
            "credential_id",
            uuid4() if credential_id is None else credential_id,
        )
        object.__setattr__(
            self,
            "generation",
            validate_ssh_credential_generation(generation),
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        """Require strongly typed endpoint and credential identifiers."""
        if not isinstance(self.endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        if not isinstance(self.credential_id, UUID):
            raise ValueError("SSH credential ID must be a UUID")
        if self.account_id is not None and not isinstance(self.account_id, UUID):
            raise ValueError("SSH account ID must be a UUID")


@dataclass(frozen=True, slots=True)
class SshProfileCredentialPreference:
    """A saved-profile login placed first in the chooser for one endpoint."""

    profile_id: UUID
    endpoint: SshEndpoint
    credential_id: UUID

    def __post_init__(self) -> None:
        """Require stable typed identifiers and an explicit endpoint binding."""
        if not isinstance(self.profile_id, UUID):
            raise ValueError("SSH credential preference profile ID must be a UUID")
        if not isinstance(self.endpoint, SshEndpoint):
            raise ValueError("SSH credential preference endpoint must be an SshEndpoint")
        if not isinstance(self.credential_id, UUID):
            raise ValueError("SSH credential preference credential ID must be a UUID")


@dataclass(frozen=True, slots=True)
class SshEndpointCredentialPreference:
    """A Quick Connect chooser preference scoped to one literal endpoint."""

    endpoint: SshEndpoint
    credential_id: UUID

    def __post_init__(self) -> None:
        """Require strongly typed endpoint and credential identifiers."""
        if not isinstance(self.endpoint, SshEndpoint):
            raise ValueError("SSH credential preference endpoint must be an SshEndpoint")
        if not isinstance(self.credential_id, UUID):
            raise ValueError("SSH credential preference credential ID must be a UUID")


@dataclass(frozen=True, slots=True)
class SshCredentialIndex:
    """One immutable, internally consistent snapshot of non-secret metadata."""

    credentials: tuple[SshCredentialRef, ...] = ()
    profile_preferences: tuple[SshProfileCredentialPreference, ...] = ()
    endpoint_preferences: tuple[SshEndpointCredentialPreference, ...] = ()
    accounts: tuple[SshAccount, ...] = ()
    _credentials_by_id: Mapping[UUID, SshCredentialRef] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )
    _preferences_by_profile_id: Mapping[UUID, UUID] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )
    _preferences_by_endpoint: Mapping[SshEndpoint, UUID] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        """Reject duplicates, dangling preferences, and unbounded snapshots."""
        # Local import avoids an identity-validator/model import cycle.
        from termana.models.ssh_account import SshAccount

        if (type(self.accounts) is not tuple or len(self.accounts) > MAX_SSH_CREDENTIALS
                or any(type(a) is not SshAccount or a.generation < 1 for a in self.accounts)
                or len({a.account_id for a in self.accounts}) != len(self.accounts)):
            raise ValueError("Invalid reusable SSH accounts")
        accounts = {a.account_id: a for a in self.accounts}
        if type(self.credentials) is not tuple:
            raise ValueError("SSH credentials must be a tuple")
        if type(self.profile_preferences) is not tuple:
            raise ValueError("SSH profile credential preferences must be a tuple")
        if type(self.endpoint_preferences) is not tuple:
            raise ValueError("SSH endpoint credential preferences must be a tuple")
        if any(not isinstance(item, SshCredentialRef) for item in self.credentials):
            raise ValueError("SSH credential index contains an unsupported credential")
        if any(
            not isinstance(item, SshProfileCredentialPreference)
            for item in self.profile_preferences
        ):
            raise ValueError("SSH credential index contains an unsupported profile preference")
        if any(
            not isinstance(item, SshEndpointCredentialPreference)
            for item in self.endpoint_preferences
        ):
            raise ValueError("SSH credential index contains an unsupported endpoint preference")
        if len(self.credentials) > MAX_SSH_CREDENTIALS:
            raise ValueError("SSH credential index contains too many credentials")
        if (
            len(self.profile_preferences) + len(self.endpoint_preferences)
            > MAX_SSH_CREDENTIAL_PREFERENCES
        ):
            raise ValueError("SSH credential index contains too many preferences")

        credential_ids = [item.credential_id for item in self.credentials]
        if len(credential_ids) != len(set(credential_ids)):
            raise ValueError("SSH credential IDs must be unique")
        if set(accounts) & set(credential_ids):
            raise ValueError("Account and credential IDs must be distinct")
        for item in self.credentials:
            if item.account_id is not None:
                account = accounts.get(item.account_id)
                if account is None or (account.username, account.generation) != (
                    item.username, item.generation
                ):
                    raise ValueError("SSH account reference is stale")
        identities = [(item.endpoint, item.username, item.account_id) for item in self.credentials]
        if len(identities) != len(set(identities)):
            raise ValueError("SSH endpoint and username pairs must be unique")

        profile_ids = [item.profile_id for item in self.profile_preferences]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("SSH credential preferences must be unique per profile")
        preferred_endpoints = [item.endpoint for item in self.endpoint_preferences]
        if len(preferred_endpoints) != len(set(preferred_endpoints)):
            raise ValueError("SSH credential preferences must be unique per endpoint")
        credential_id_set = set(credential_ids)
        all_preferences = (*self.profile_preferences, *self.endpoint_preferences)
        if any(item.credential_id not in credential_id_set for item in all_preferences):
            raise ValueError("SSH credential preference references an unknown credential")
        credentials_by_id = {item.credential_id: item for item in self.credentials}
        if any(
            credentials_by_id[item.credential_id].endpoint != item.endpoint
            for item in all_preferences
        ):
            raise ValueError("SSH credential preference belongs to a different endpoint")

        object.__setattr__(
            self,
            "_credentials_by_id",
            MappingProxyType(credentials_by_id),
        )
        object.__setattr__(
            self,
            "_preferences_by_profile_id",
            MappingProxyType(
                {item.profile_id: item.credential_id for item in self.profile_preferences}
            ),
        )
        object.__setattr__(
            self,
            "_preferences_by_endpoint",
            MappingProxyType(
                {item.endpoint: item.credential_id for item in self.endpoint_preferences}
            ),
        )

    def credential(self, credential_id: UUID) -> SshCredentialRef | None:
        """Return a reference by ID without exposing its password."""
        return self._credentials_by_id.get(credential_id)

    def credentials_for(self, endpoint: SshEndpoint) -> tuple[SshCredentialRef, ...]:
        """Return saved logins for an exact literal endpoint in stable order."""
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential endpoint must be an SshEndpoint")
        return tuple(item for item in self.credentials
                     if item.endpoint == endpoint and item.account_id is None)

    def preferred_for_profile(
        self,
        profile_id: UUID,
        endpoint: SshEndpoint,
    ) -> SshCredentialRef | None:
        """Resolve a saved-profile preference only while its endpoint still matches."""
        if not isinstance(profile_id, UUID):
            raise ValueError("SSH credential preference profile ID must be a UUID")
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential preference endpoint must be an SshEndpoint")
        credential_id = self._preferences_by_profile_id.get(profile_id)
        if credential_id is None:
            return None
        credential = self._credentials_by_id[credential_id]
        return credential if credential.endpoint == endpoint else None

    def preferred_for_endpoint(self, endpoint: SshEndpoint) -> SshCredentialRef | None:
        """Resolve the login shown first for an ephemeral Quick Connect endpoint."""
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential preference endpoint must be an SshEndpoint")
        credential_id = self._preferences_by_endpoint.get(endpoint)
        if credential_id is None:
            return None
        return self._credentials_by_id[credential_id]

    def with_credential(self, credential: SshCredentialRef) -> SshCredentialIndex:
        """Return a snapshot containing one new or identically keyed reference."""
        if not isinstance(credential, SshCredentialRef):
            raise ValueError("Expected an SshCredentialRef")
        existing = self.credential(credential.credential_id)
        if existing is None:
            return replace(self, credentials=(*self.credentials, credential))
        updated = tuple(
            credential if item.credential_id == credential.credential_id else item
            for item in self.credentials
        )
        return replace(self, credentials=updated)

    def without_credentials(self, credential_ids: frozenset[UUID]) -> SshCredentialIndex:
        """Remove references and every profile preference that points to them."""
        if not isinstance(credential_ids, frozenset) or any(
            not isinstance(item, UUID) for item in credential_ids
        ):
            raise ValueError("SSH credential IDs must be a frozenset of UUIDs")
        return replace(
            self,
            credentials=tuple(
                item for item in self.credentials if item.credential_id not in credential_ids
            ),
            profile_preferences=tuple(
                item
                for item in self.profile_preferences
                if item.credential_id not in credential_ids
            ),
            endpoint_preferences=tuple(
                item
                for item in self.endpoint_preferences
                if item.credential_id not in credential_ids
            ),
        )

    def with_profile_preference(
        self,
        profile_id: UUID,
        endpoint: SshEndpoint,
        credential_id: UUID | None,
    ) -> SshCredentialIndex:
        """Atomically set or clear one profile's preferred credential reference."""
        if not isinstance(profile_id, UUID):
            raise ValueError("SSH credential preference profile ID must be a UUID")
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential preference endpoint must be an SshEndpoint")
        remaining = tuple(
            item for item in self.profile_preferences if item.profile_id != profile_id
        )
        if credential_id is None:
            return replace(self, profile_preferences=remaining)
        if not isinstance(credential_id, UUID):
            raise ValueError("SSH credential preference credential ID must be a UUID")
        credential = self._credentials_by_id.get(credential_id)
        if credential is None:
            raise ValueError("SSH credential preference references an unknown credential")
        if credential.endpoint != endpoint:
            raise ValueError("SSH credential preference belongs to a different endpoint")
        return replace(
            self,
            profile_preferences=(
                *remaining,
                SshProfileCredentialPreference(
                    profile_id=profile_id,
                    endpoint=endpoint,
                    credential_id=credential_id,
                ),
            ),
        )

    def with_endpoint_preference(
        self,
        endpoint: SshEndpoint,
        credential_id: UUID | None,
    ) -> SshCredentialIndex:
        """Atomically set or clear a Quick Connect endpoint preference."""
        if not isinstance(endpoint, SshEndpoint):
            raise ValueError("SSH credential preference endpoint must be an SshEndpoint")
        remaining = tuple(item for item in self.endpoint_preferences if item.endpoint != endpoint)
        if credential_id is None:
            return replace(self, endpoint_preferences=remaining)
        if not isinstance(credential_id, UUID):
            raise ValueError("SSH credential preference credential ID must be a UUID")
        credential = self._credentials_by_id.get(credential_id)
        if credential is None:
            raise ValueError("SSH credential preference references an unknown credential")
        if credential.endpoint != endpoint:
            raise ValueError("SSH credential preference belongs to a different endpoint")
        return replace(
            self,
            endpoint_preferences=(
                *remaining,
                SshEndpointCredentialPreference(
                    endpoint=endpoint,
                    credential_id=credential_id,
                ),
            ),
        )
