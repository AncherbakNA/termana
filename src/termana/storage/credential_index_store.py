"""Strict atomic persistence for non-secret SSH credential metadata."""

import json
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from termana.models.ssh_account import SshAccount
from termana.models.ssh_credential import (
    MAX_SSH_CREDENTIAL_PREFERENCES,
    MAX_SSH_CREDENTIALS,
    SshCredentialIndex,
    SshCredentialRef,
    SshEndpoint,
    SshEndpointCredentialPreference,
    SshProfileCredentialPreference,
)
from termana.storage.atomic_file import replace_file_atomically
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.storage.settings_migration_backup import (
    SettingsMigrationBackupError,
    preserve_before_schema_change,
)

CREDENTIAL_INDEX_SCHEMA_VERSION: Final = 3
LEGACY_CREDENTIAL_INDEX_SCHEMA_VERSION: Final = 1
MAX_CREDENTIAL_INDEX_FILE_BYTES: Final = 512 * 1024
DOCUMENT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "credentials",
        "profile_preferences",
        "endpoint_preferences",
    }
)
LEGACY_CREDENTIAL_FIELDS: Final = frozenset({"id", "endpoint", "username"})
CREDENTIAL_FIELDS: Final = frozenset({"id", "endpoint", "username", "generation"})
ENDPOINT_FIELDS: Final = frozenset({"host", "port"})
PROFILE_PREFERENCE_FIELDS: Final = frozenset({"profile_id", "endpoint", "credential_id"})
ENDPOINT_PREFERENCE_FIELDS: Final = frozenset({"endpoint", "credential_id"})
FAILED_LOAD_WRITE_DISABLED_REASON: Final = (
    "SSH credential metadata could not be loaded safely and cannot be overwritten"
)


class CredentialIndexStoreError(RuntimeError):
    """Raised when non-secret credential metadata cannot be handled safely."""


class CredentialIndexStore:
    """Load and atomically save one complete SSH credential index snapshot."""

    def __init__(self, path: Path) -> None:
        """Retain the explicit metadata path without touching the filesystem."""
        if not isinstance(path, Path):
            raise TypeError("Credential index path must be a Path")
        self.path = path
        self._write_disabled_reason: str | None = None

    def load(self) -> SshCredentialIndex:
        """Load a validated snapshot or return an empty first-run index."""
        try:
            with self.path.open("rb") as metadata_file:
                raw_document = metadata_file.read(MAX_CREDENTIAL_INDEX_FILE_BYTES + 1)
        except FileNotFoundError:
            self._write_disabled_reason = None
            return SshCredentialIndex()
        except OSError as error:
            self._disable_writes_after_failed_load()
            raise CredentialIndexStoreError(
                f"Could not read SSH credential metadata at {self.path}: {error}"
            ) from error

        try:
            if len(raw_document) > MAX_CREDENTIAL_INDEX_FILE_BYTES:
                raise ValueError("credential metadata file exceeds the supported size limit")
            document = json.loads(
                raw_document.decode("utf-8"),
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            self._disable_writes_after_failed_load()
            raise CredentialIndexStoreError(
                f"Could not read SSH credential metadata at {self.path}: {error}"
            ) from error

        if (
            isinstance(document, dict)
            and type(document.get("schema_version")) is int
            and document["schema_version"] > CREDENTIAL_INDEX_SCHEMA_VERSION
        ):
            self._write_disabled_reason = (
                "SSH credential metadata was created by a newer Termana version "
                "and cannot be overwritten"
            )

        try:
            index = self._index_from_document(document)
        except CredentialIndexStoreError:
            self._disable_writes_after_failed_load()
            raise
        except (TypeError, ValueError, RecursionError) as error:
            self._disable_writes_after_failed_load()
            raise CredentialIndexStoreError(
                f"Could not read SSH credential metadata at {self.path}: {error}"
            ) from error
        self._write_disabled_reason = None
        return index

    def save(self, index: SshCredentialIndex) -> None:
        """Persist a complete consistent snapshot using same-directory replace."""
        if self._write_disabled_reason is not None:
            raise CredentialIndexStoreError(self._write_disabled_reason)
        if not isinstance(index, SshCredentialIndex):
            raise CredentialIndexStoreError("Expected an SshCredentialIndex snapshot")

        document = {
            "schema_version": CREDENTIAL_INDEX_SCHEMA_VERSION,
            "credentials": [
                {
                    "id": str(item.credential_id),
                    "endpoint": self._endpoint_to_document(item.endpoint),
                    "username": item.username,
                    "generation": item.generation,
                    "account_id": str(item.account_id) if item.account_id else None,
                }
                for item in index.credentials
            ],
            "accounts": [dict(id=str(a.account_id), name=a.name, username=a.username,
                              generation=a.generation) for a in index.accounts],
            "profile_preferences": [
                {
                    "profile_id": str(item.profile_id),
                    "endpoint": self._endpoint_to_document(item.endpoint),
                    "credential_id": str(item.credential_id),
                }
                for item in index.profile_preferences
            ],
            "endpoint_preferences": [
                {
                    "endpoint": self._endpoint_to_document(item.endpoint),
                    "credential_id": str(item.credential_id),
                }
                for item in index.endpoint_preferences
            ],
        }
        try:
            serialized = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(serialized) > MAX_CREDENTIAL_INDEX_FILE_BYTES:
                raise ValueError("credential metadata file exceeds the supported size limit")
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise CredentialIndexStoreError(
                f"Could not write SSH credential metadata at {self.path}: {error}"
            ) from error

        try:
            prepared = preserve_before_schema_change(
                self.path, current_version=CREDENTIAL_INDEX_SCHEMA_VERSION,
                limit=MAX_CREDENTIAL_INDEX_FILE_BYTES, validate=self._index_from_document,
                label="ssh-credential-index",
            )
            prepared.assert_unchanged()
            replace_file_atomically(self.path, serialized)
        except (OSError, SettingsMigrationBackupError) as error:
            raise CredentialIndexStoreError(
                f"Could not write SSH credential metadata at {self.path}: {error}"
            ) from error

    def _disable_writes_after_failed_load(self) -> None:
        """Preserve corrupt or unknown data after any unsafe load result."""
        if self._write_disabled_reason is None:
            self._write_disabled_reason = FAILED_LOAD_WRITE_DISABLED_REASON

    @classmethod
    def _index_from_document(cls, document: Any) -> SshCredentialIndex:
        """Validate every field before constructing the immutable index."""
        if not isinstance(document, dict):
            raise CredentialIndexStoreError("SSH credential metadata must contain a JSON object")
        expected_fields = (DOCUMENT_FIELDS | {"accounts"}
                           if document.get("schema_version") == 3 else DOCUMENT_FIELDS)
        if set(document) != expected_fields:
            raise CredentialIndexStoreError(
                "SSH credential metadata fields do not match its schema"
            )
        schema_version = document.get("schema_version")
        if type(schema_version) is not int or schema_version not in {
            LEGACY_CREDENTIAL_INDEX_SCHEMA_VERSION,
            2,
            CREDENTIAL_INDEX_SCHEMA_VERSION,
        }:
            raise CredentialIndexStoreError("Unsupported SSH credential metadata schema version")

        credentials_value = cls._bounded_list(
            document.get("credentials"),
            field_name="credentials",
            maximum_length=MAX_SSH_CREDENTIALS,
        )
        credentials: list[SshCredentialRef] = []
        credential_fields = (
            LEGACY_CREDENTIAL_FIELDS
            if schema_version == LEGACY_CREDENTIAL_INDEX_SCHEMA_VERSION
            else CREDENTIAL_FIELDS
        )
        if schema_version == 3:
            credential_fields = credential_fields | {"account_id"}
        for index, value in enumerate(credentials_value, start=1):
            try:
                if not isinstance(value, dict) or set(value) != credential_fields:
                    raise ValueError("credential fields do not match the schema")
                credentials.append(
                    SshCredentialRef(
                        credential_id=cls._uuid_from_value(
                            value.get("id"),
                            field_name="credential ID",
                        ),
                        endpoint=cls._endpoint_from_value(value.get("endpoint")),
                        username=value.get("username"),
                        generation=(
                            0
                            if schema_version == LEGACY_CREDENTIAL_INDEX_SCHEMA_VERSION
                            else value.get("generation")
                        ),
                        account_id=(cls._uuid_from_value(value["account_id"],
                                                       field_name="account ID")
                                    if schema_version == 3 and value["account_id"] is not None
                                    else None),
                    )
                )
            except (TypeError, ValueError) as error:
                raise CredentialIndexStoreError(
                    f"Invalid SSH credential metadata item #{index}: {error}"
                ) from error

        profile_values = cls._bounded_list(
            document.get("profile_preferences"),
            field_name="profile_preferences",
            maximum_length=MAX_SSH_CREDENTIAL_PREFERENCES,
        )
        profile_preferences: list[SshProfileCredentialPreference] = []
        for index, value in enumerate(profile_values, start=1):
            try:
                if not isinstance(value, dict) or set(value) != PROFILE_PREFERENCE_FIELDS:
                    raise ValueError("profile preference fields do not match the schema")
                profile_preferences.append(
                    SshProfileCredentialPreference(
                        profile_id=cls._uuid_from_value(
                            value.get("profile_id"),
                            field_name="profile ID",
                        ),
                        endpoint=cls._endpoint_from_value(value.get("endpoint")),
                        credential_id=cls._uuid_from_value(
                            value.get("credential_id"),
                            field_name="credential ID",
                        ),
                    )
                )
            except (TypeError, ValueError) as error:
                raise CredentialIndexStoreError(
                    f"Invalid SSH profile preference #{index}: {error}"
                ) from error

        endpoint_values = cls._bounded_list(
            document.get("endpoint_preferences"),
            field_name="endpoint_preferences",
            maximum_length=MAX_SSH_CREDENTIAL_PREFERENCES,
        )
        if len(profile_values) + len(endpoint_values) > MAX_SSH_CREDENTIAL_PREFERENCES:
            raise CredentialIndexStoreError("SSH credential metadata contains too many preferences")
        endpoint_preferences: list[SshEndpointCredentialPreference] = []
        for index, value in enumerate(endpoint_values, start=1):
            try:
                if not isinstance(value, dict) or set(value) != ENDPOINT_PREFERENCE_FIELDS:
                    raise ValueError("endpoint preference fields do not match the schema")
                endpoint_preferences.append(
                    SshEndpointCredentialPreference(
                        endpoint=cls._endpoint_from_value(value.get("endpoint")),
                        credential_id=cls._uuid_from_value(
                            value.get("credential_id"),
                            field_name="credential ID",
                        ),
                    )
                )
            except (TypeError, ValueError) as error:
                raise CredentialIndexStoreError(
                    f"Invalid SSH endpoint preference #{index}: {error}"
                ) from error

        try:
            accounts = []
            if schema_version == 3:
                for value in cls._bounded_list(document["accounts"], field_name="accounts",
                                                maximum_length=MAX_SSH_CREDENTIALS):
                    if type(value) is not dict or set(value) != {
                        "id", "name", "username", "generation"
                    }:
                        raise ValueError("Invalid SSH account fields")
                    accounts.append(SshAccount(value["name"], value["username"],
                                               cls._uuid_from_value(value["id"],
                                                                    field_name="account ID"),
                                               value["generation"]))
            return SshCredentialIndex(
                credentials=tuple(credentials),
                profile_preferences=tuple(profile_preferences),
                endpoint_preferences=tuple(endpoint_preferences),
                accounts=tuple(accounts),
            )
        except ValueError as error:
            raise CredentialIndexStoreError(
                f"Invalid SSH credential metadata index: {error}"
            ) from error

    @staticmethod
    def _endpoint_to_document(endpoint: SshEndpoint) -> dict[str, object]:
        """Convert one exact endpoint identity into JSON-compatible fields."""
        return {"host": endpoint.host, "port": endpoint.port}

    @staticmethod
    def _endpoint_from_value(value: Any) -> SshEndpoint:
        """Validate a complete endpoint object without applying defaults."""
        if not isinstance(value, dict) or set(value) != ENDPOINT_FIELDS:
            raise ValueError("endpoint fields do not match the schema")
        port = value.get("port")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("SSH credential port must be an integer")
        return SshEndpoint(host=value.get("host"), port=port)

    @staticmethod
    def _bounded_list(value: Any, *, field_name: str, maximum_length: int) -> list[Any]:
        """Require an array before iterating and constructing nested objects."""
        if not isinstance(value, list):
            raise CredentialIndexStoreError(f"SSH credential metadata {field_name} must be a list")
        if len(value) > maximum_length:
            raise CredentialIndexStoreError(
                f"SSH credential metadata contains too many {field_name}"
            )
        return value

    @staticmethod
    def _uuid_from_value(value: Any, *, field_name: str) -> UUID:
        """Decode only canonical lower-case UUID strings."""
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError(f"{field_name} must use canonical UUID format")
        return parsed
