"""JSON persistence for connection profiles."""

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from termana.models.connection import (
    MAX_CONNECTION_CATALOG_ITEMS,
    MAX_CONNECTION_FOLDERS,
    ConnectionCatalog,
    ConnectionFolder,
    ConnectionProfile,
    ConnectionProtocol,
    TelnetEncoding,
    WordDeleteMode,
)
from termana.models.rdp_options import RdpOptions
from termana.models.ssh_algorithms import (
    SshAlgorithms,
    algorithms_from_document,
    algorithms_to_document,
)
from termana.models.ssh_identity import SshIdentityOptions
from termana.models.ssh_jump import SshJumpOptions
from termana.models.ssh_options import SshConnectionOptions
from termana.models.ssh_tunnels import SshTunnelOptions
from termana.storage.atomic_file import replace_file_atomically
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.storage.rdp_options_codec import rdp_options_from_document, rdp_options_to_document
from termana.storage.settings_migration_backup import (
    SettingsMigrationBackupError,
    preserve_before_schema_change,
)
from termana.storage.ssh_identity_codec import (
    ssh_identity_from_document,
    ssh_identity_to_document,
)
from termana.storage.ssh_jump_codec import ssh_jump_from_document, ssh_jump_to_document
from termana.storage.ssh_options_codec import (
    ssh_options_from_document,
    ssh_options_to_document,
)
from termana.storage.ssh_tunnels_codec import ssh_tunnels_from_document, ssh_tunnels_to_document

SCHEMA_VERSION: Final = 16
MAX_CONNECTION_FILE_BYTES: Final = 4 * 1024 * 1024
DOCUMENT_FIELDS_V1: Final = frozenset({"schema_version", "connections"})
DOCUMENT_FIELDS_V2: Final = frozenset({"schema_version", "root_items", "folders", "connections"})
DOCUMENT_FIELDS_V3: Final = DOCUMENT_FIELDS_V2
DOCUMENT_FIELDS_V4: Final = DOCUMENT_FIELDS_V3
DOCUMENT_FIELDS_V5: Final = DOCUMENT_FIELDS_V4
DOCUMENT_FIELDS_V6: Final = DOCUMENT_FIELDS_V5
DOCUMENT_FIELDS_V7: Final = DOCUMENT_FIELDS_V6
DOCUMENT_FIELDS_V8: Final = DOCUMENT_FIELDS_V7
DOCUMENT_FIELDS_V9: Final = DOCUMENT_FIELDS_V8
PROFILE_FIELDS_V1_V2: Final = frozenset({"id", "name", "protocol", "host", "port", "username"})
PROFILE_FIELDS_V3: Final = PROFILE_FIELDS_V1_V2 | {"telnet_encoding"}
PROFILE_FIELDS_V4: Final = PROFILE_FIELDS_V3 | {"ssh_cipher"}
PROFILE_FIELDS_V5: Final = PROFILE_FIELDS_V4 | {"ssh_options"}
PROFILE_FIELDS_V6: Final = PROFILE_FIELDS_V5 | {"is_favorite"}
PROFILE_FIELDS_V7: Final = PROFILE_FIELDS_V6 | {"ssh_identity"}
PROFILE_FIELDS_V8: Final = PROFILE_FIELDS_V7 | {"ssh_jump"}
PROFILE_FIELDS_V9: Final = PROFILE_FIELDS_V8 | {"ssh_tunnels"}
PROFILE_FIELDS_V10: Final = PROFILE_FIELDS_V9 | {"ssh_algorithms", "ssh_account_id"}
PROFILE_FIELDS_V12: Final = PROFILE_FIELDS_V10 | {"rdp_options"}
PROFILE_FIELDS_V14: Final = PROFILE_FIELDS_V12 | {"word_delete_mode"}
PROFILE_FIELDS_V15: Final = PROFILE_FIELDS_V14 | {"accent_color"}
DOCUMENT_FIELDS_BY_SCHEMA: Final = MappingProxyType(
    {
        2: DOCUMENT_FIELDS_V2,
        3: DOCUMENT_FIELDS_V3,
        4: DOCUMENT_FIELDS_V4,
        5: DOCUMENT_FIELDS_V5,
        6: DOCUMENT_FIELDS_V6,
        7: DOCUMENT_FIELDS_V7,
        8: DOCUMENT_FIELDS_V8,
        9: DOCUMENT_FIELDS_V9,
        10: DOCUMENT_FIELDS_V9,
        11: DOCUMENT_FIELDS_V9,
        12: DOCUMENT_FIELDS_V9,
        13: DOCUMENT_FIELDS_V9,
        14: DOCUMENT_FIELDS_V9,
        15: DOCUMENT_FIELDS_V9,
        16: DOCUMENT_FIELDS_V9 | {"sync_state"},
    }
)
PROFILE_FIELDS_BY_SCHEMA: Final = MappingProxyType(
    {
        1: PROFILE_FIELDS_V1_V2,
        2: PROFILE_FIELDS_V1_V2,
        3: PROFILE_FIELDS_V3,
        4: PROFILE_FIELDS_V4,
        5: PROFILE_FIELDS_V5,
        6: PROFILE_FIELDS_V6,
        7: PROFILE_FIELDS_V7,
        8: PROFILE_FIELDS_V8,
        9: PROFILE_FIELDS_V9,
        10: PROFILE_FIELDS_V10,
        11: PROFILE_FIELDS_V10,
        12: PROFILE_FIELDS_V12,
        13: PROFILE_FIELDS_V12,
        14: PROFILE_FIELDS_V14,
        15: PROFILE_FIELDS_V15,
        16: PROFILE_FIELDS_V15,
    }
)
FOLDER_FIELDS: Final = frozenset({"id", "name", "children"})
FAILED_LOAD_WRITE_DISABLED_REASON: Final = (
    "Connections could not be loaded safely and cannot be overwritten"
)


class ConnectionStoreError(RuntimeError):
    """Raised when connection profiles cannot be loaded or saved safely."""


class ConnectionStore:
    """Load and atomically save connection profiles in a JSON file."""

    def __init__(self, path: Path) -> None:
        """Store the target file path without accessing the file system yet."""
        self.path = path
        self._write_disabled_reason: str | None = None
        self._sync_state = None
        self._loaded_catalog = None
        self._loaded_bytes = None
        self._has_loaded = False

    @property
    def sync_state(self):
        """Non-secret local metadata; never part of portable exports."""
        return self._sync_state

    def load(self) -> ConnectionCatalog:
        """Load a complete catalog, returning an empty tree when no file exists."""
        try:
            with self.path.open("rb") as file:
                raw_document = file.read(MAX_CONNECTION_FILE_BYTES + 1)
        except FileNotFoundError:
            self._write_disabled_reason = None
            self._sync_state = None
            self._loaded_catalog = ConnectionCatalog()
            self._loaded_bytes, self._has_loaded = None, True
            return self._loaded_catalog
        except OSError as error:
            self._disable_writes_after_failed_load()
            raise ConnectionStoreError(f"Could not read {self.path}: {error}") from error

        try:
            if len(raw_document) > MAX_CONNECTION_FILE_BYTES:
                raise ValueError("connection file exceeds the supported size limit")
            document = json.loads(
                raw_document.decode("utf-8"),
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            self._disable_writes_after_failed_load()
            raise ConnectionStoreError(f"Could not read {self.path}: {error}") from error

        if (
            isinstance(document, dict)
            and type(document.get("schema_version")) is int
            and document["schema_version"] > SCHEMA_VERSION
        ):
            self._write_disabled_reason = (
                "Connections were created by a newer Termana version and cannot be overwritten"
            )

        try:
            catalog = self._catalog_from_document(document)
        except ConnectionStoreError:
            self._disable_writes_after_failed_load()
            raise
        except (TypeError, ValueError, RecursionError) as error:
            self._disable_writes_after_failed_load()
            raise ConnectionStoreError(f"Could not read {self.path}: {error}") from error
        self._write_disabled_reason = None
        self._sync_state = self._sync_from_document(document, catalog)
        self._loaded_catalog, self._loaded_bytes, self._has_loaded = catalog, raw_document, True
        return catalog

    def save(self, catalog: ConnectionCatalog) -> None:
        """Write one complete catalog through a temporary file and atomic replace."""
        if self._write_disabled_reason is not None:
            raise ConnectionStoreError(self._write_disabled_reason)
        # A freshly constructed writer must not accidentally erase an existing
        # sync binding. Normal application ownership already serializes writers.
        if not self._has_loaded and self.path.exists():
            self.load()
        try:
            state = None if self._sync_state is None else self._sync_state.reconciled(catalog)
        except ValueError:
            raise ConnectionStoreError("Connection sync state could not be updated safely") from None
        self._save(catalog, state)

    def save_sync(self, plan, state) -> None:
        """Commit catalog/map/base as ONE file, after a private exact-byte backup.

        The GUI owner must serialize this with ordinary catalog mutations and
        publish its in-memory catalog only after successful return. The app data
        instance lock is mandatory; this is not a lock-free cross-process API.
        """
        from termana.profile_sync.application import PreparedSyncApplication
        from termana.profile_sync.local_state import LocalSyncState
        from termana.profile_sync.model import SyncError, encode_snapshot, project_catalog

        if type(plan) is not PreparedSyncApplication or type(state) is not LocalSyncState:
            raise ConnectionStoreError("A prepared synchronization is required")
        if self._write_disabled_reason is not None:
            raise ConnectionStoreError(self._write_disabled_reason)
        try:
            if not self._has_loaded:
                raise SyncError("Load connections before synchronization")
            plan.check_current(self._loaded_catalog, plan.source_identities if self._sync_state is None
                               else self._sync_state.identities)
            if state.identities != plan.identities:
                raise SyncError("Sync state does not match the prepared catalog")
            state.validate_catalog(plan.catalog)
            if self._sync_state is not None and (
                state.repository != self._sync_state.repository
                or state.repository_id != self._sync_state.repository_id
                or state.account_id != self._sync_state.account_id
                or state.secret_id != self._sync_state.secret_id
            ):
                raise SyncError("Disconnect before changing the sync account or repository")
            if state.base is None:
                if plan.catalog != plan.source_catalog:
                    raise SyncError("A remote merge base is required for catalog changes")
            else:
                projected, _ = project_catalog(plan.catalog, plan.identities)
                if encode_snapshot(projected) != encode_snapshot(state.base):
                    raise SyncError("The accepted remote base must match the committed catalog")
            self._assert_sync_source()
            if state == self._sync_state and plan.catalog == self._loaded_catalog:
                return  # Validated no-op: don't churn private backups or file generations.
            self._backup_before_sync()
            self._assert_sync_source()
            self._save(plan.catalog, state)
        except (OSError, ValueError, ConnectionStoreError):
            # A failure can occur AFTER rename. Do not claim old bytes survived
            # or let an ordinary edit erase the now-unknown sync generation.
            self._write_disabled_reason = "Synchronization could not be confirmed; reload saved connections before saving"
            raise ConnectionStoreError(self._write_disabled_reason) from None

    def disconnect_sync(self):
        """Forget this device's binding only; never delete the remote repository."""
        if self._write_disabled_reason is not None or not self._has_loaded:
            raise ConnectionStoreError("Reload saved connections before disconnecting sync")
        try:
            self._assert_sync_source()
            self._backup_before_sync()
            self._assert_sync_source()
            self._save(self._loaded_catalog, None)
        except Exception:
            self._write_disabled_reason = "Sync disconnect could not be confirmed; reload saved connections"
            raise ConnectionStoreError(self._write_disabled_reason) from None

    def _assert_sync_source(self):
        from termana.storage.configuration_backup import _read

        current = _read(self.path, MAX_CONNECTION_FILE_BYTES, missing=True)
        if not self._has_loaded or current != self._loaded_bytes:
            raise ConnectionStoreError("Connections changed on disk; reload before synchronization")

    def _backup_before_sync(self):
        from uuid import uuid4

        from termana.terminal.private_recovery_files import PrivateDirectory, PrivateFile

        if self._loaded_bytes is None:
            return
        # The existing DataOwnership handle already pins this parent against
        # deletion. Reopen metadata-only with DELETE sharing; requesting another
        # DELETE/no-delete-sharing handle conflicts with that owner on Windows.
        # Child creation remains relative to this validated no-reparse parent.
        with PrivateDirectory(self.path.parent) as parent:
            path = self.path.parent / "sync-backups"
            with PrivateDirectory(path, parent=parent, create=not path.exists(), writable=True) as backups:
                name = "connections-" + uuid4().hex + ".json"
                with PrivateFile(backups, name, create=True) as target:
                    target.rewrite(self._loaded_bytes)
                backups.sync()
            parent.sync()

    def _save(self, catalog, sync_state):
        if self._write_disabled_reason is not None:
            raise ConnectionStoreError(self._write_disabled_reason)
        if not isinstance(catalog, ConnectionCatalog):
            raise ConnectionStoreError("Expected a ConnectionCatalog snapshot")
        if sync_state is not None:
            from termana.terminal.private_recovery_files import PrivateDirectory

            try:
                with PrivateDirectory(self.path.parent):
                    pass
            except (OSError, ValueError):
                raise ConnectionStoreError("Synchronization requires a private application data folder") from None

        document = {
            "schema_version": SCHEMA_VERSION,
            "root_items": [str(item_id) for item_id in catalog.root_item_ids],
            "folders": [self._folder_to_document(folder) for folder in catalog.folders],
            "connections": [self._profile_to_document(profile) for profile in catalog.profiles],
            "sync_state": None if sync_state is None else sync_state.document(),
        }
        try:
            serialized = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(serialized) > MAX_CONNECTION_FILE_BYTES:
                raise ValueError("connection file exceeds the supported size limit")
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise ConnectionStoreError(f"Could not write {self.path}: {error}") from error

        try:
            prepared = preserve_before_schema_change(
                self.path, current_version=SCHEMA_VERSION, limit=MAX_CONNECTION_FILE_BYTES,
                validate=self._catalog_from_document, label="connections",
            )
            prepared.assert_unchanged()
            if self._sync_state is not None:
                self._assert_sync_source()
            replace_file_atomically(self.path, serialized)
        except (OSError, SettingsMigrationBackupError) as error:
            raise ConnectionStoreError(f"Could not write {self.path}: {error}") from error
        self._sync_state, self._loaded_catalog = sync_state, catalog
        self._loaded_bytes, self._has_loaded = serialized, True

    @staticmethod
    def _sync_from_document(document, catalog):
        from termana.profile_sync.local_state import LocalSyncState
        from termana.profile_sync.model import SyncError

        if document["schema_version"] < 16 or document["sync_state"] is None:
            return None
        try:
            state = LocalSyncState.from_document(document["sync_state"])
            state.validate_catalog(catalog)
            return state
        except SyncError:
            raise ConnectionStoreError("Connection sync metadata is invalid; saved data cannot be overwritten") from None

    def _disable_writes_after_failed_load(self) -> None:
        """Latch a conservative session guard without replacing a specific reason."""
        if self._write_disabled_reason is None:
            self._write_disabled_reason = FAILED_LOAD_WRITE_DISABLED_REASON

    @staticmethod
    def _profile_to_document(profile: ConnectionProfile) -> dict[str, object]:
        """Convert a profile into JSON-compatible values."""
        return {
            "id": str(profile.profile_id),
            "name": profile.name,
            "protocol": profile.protocol.value,
            "host": profile.host,
            "port": profile.port,
            "telnet_encoding": profile.telnet_encoding.value,
            "ssh_cipher": profile.ssh_cipher,
            "ssh_options": ssh_options_to_document(profile.ssh_options),
            "is_favorite": profile.is_favorite,
            "ssh_identity": ssh_identity_to_document(profile.ssh_identity),
            "ssh_jump": ssh_jump_to_document(profile.ssh_jump),
            "ssh_tunnels": ssh_tunnels_to_document(profile.ssh_tunnels),
            "ssh_algorithms": algorithms_to_document(profile.ssh_algorithms),
            "ssh_account_id": str(profile.ssh_account_id) if profile.ssh_account_id else None,
            "rdp_options": rdp_options_to_document(profile.rdp_options),
            "word_delete_mode": profile.word_delete_mode.value,
            "accent_color": profile.accent_color,
            # Retained as a null compatibility field since schema v3. Session profiles
            # no longer own an SSH login name; it is selected when connecting.
            "username": None,
        }

    @staticmethod
    def _folder_to_document(folder: ConnectionFolder) -> dict[str, object]:
        """Convert one folder and its ordered children into JSON values."""
        return {
            "id": str(folder.folder_id),
            "name": folder.name,
            "children": [str(child_id) for child_id in folder.child_ids],
        }

    @classmethod
    def _catalog_from_document(cls, document: Any) -> ConnectionCatalog:
        """Validate a complete versioned document and build an immutable tree."""
        if not isinstance(document, dict):
            raise ConnectionStoreError("Connection file must contain a JSON object")
        schema_version = document.get("schema_version")
        if type(schema_version) is not int:
            raise ConnectionStoreError("Unsupported connection file schema version")
        if schema_version == 1:
            return cls._catalog_from_v1(document)
        if schema_version in DOCUMENT_FIELDS_BY_SCHEMA:
            return cls._catalog_from_tree(document, schema_version=schema_version)
        raise ConnectionStoreError("Unsupported connection file schema version")

    @classmethod
    def _catalog_from_v1(cls, document: dict[str, Any]) -> ConnectionCatalog:
        """Migrate the original flat profile list in memory without rewriting it."""
        if set(document) != DOCUMENT_FIELDS_V1:
            raise ConnectionStoreError("Connection file fields do not match schema version 1")
        profiles = cls._profiles_from_value(document.get("connections"), schema_version=1)
        try:
            return ConnectionCatalog(
                root_item_ids=tuple(profile.profile_id for profile in profiles),
                profiles=profiles,
            )
        except ValueError as error:
            raise ConnectionStoreError(f"Invalid connection catalog: {error}") from error

    @classmethod
    def _catalog_from_tree(
        cls,
        document: dict[str, Any],
        *,
        schema_version: int,
    ) -> ConnectionCatalog:
        """Decode a normalized folder tree, migrating older profile fields in memory."""
        expected_document_fields = DOCUMENT_FIELDS_BY_SCHEMA[schema_version]
        if set(document) != expected_document_fields:
            raise ConnectionStoreError(
                f"Connection file fields do not match schema version {schema_version}"
            )

        root_items = document.get("root_items")
        if not isinstance(root_items, list):
            raise ConnectionStoreError("Connection file must contain a root_items list")
        if len(root_items) > MAX_CONNECTION_CATALOG_ITEMS:
            raise ConnectionStoreError("Connection catalog contains too many root items")
        root_item_ids = tuple(
            cls._uuid_from_value(item_id, field_name=f"root item #{index}")
            for index, item_id in enumerate(root_items, start=1)
        )

        folders_value = document.get("folders")
        if not isinstance(folders_value, list):
            raise ConnectionStoreError("Connection file must contain a folders list")
        if len(folders_value) > MAX_CONNECTION_FOLDERS:
            raise ConnectionStoreError("Connection catalog contains too many folders")
        folders: list[ConnectionFolder] = []
        for index, folder_value in enumerate(folders_value, start=1):
            try:
                if not isinstance(folder_value, dict):
                    raise ValueError("folder must be a JSON object")
                if set(folder_value) != FOLDER_FIELDS:
                    raise ValueError(f"folder fields do not match schema version {schema_version}")
                children = folder_value.get("children")
                if not isinstance(children, list):
                    raise ValueError("folder children must be a list")
                if len(children) > MAX_CONNECTION_CATALOG_ITEMS:
                    raise ValueError("folder contains too many children")
                child_ids = tuple(
                    cls._uuid_from_value(
                        child_id,
                        field_name=f"folder #{index} child #{child_index}",
                    )
                    for child_index, child_id in enumerate(children, start=1)
                )
                folder = ConnectionFolder(
                    folder_id=cls._uuid_from_value(
                        folder_value.get("id"),
                        field_name=f"folder #{index} ID",
                    ),
                    name=folder_value.get("name"),
                    child_ids=child_ids,
                )
            except (TypeError, ValueError) as error:
                raise ConnectionStoreError(
                    f"Invalid connection folder #{index}: {error}"
                ) from error
            folders.append(folder)

        profiles = cls._profiles_from_value(
            document.get("connections"),
            schema_version=schema_version,
        )
        try:
            catalog = ConnectionCatalog(
                root_item_ids=root_item_ids,
                folders=tuple(folders),
                profiles=profiles,
            )
        except ValueError as error:
            raise ConnectionStoreError(f"Invalid connection catalog: {error}") from error
        cls._sync_from_document(document, catalog)
        return catalog

    @classmethod
    def _profiles_from_value(
        cls,
        connections: Any,
        *,
        schema_version: int,
    ) -> tuple[ConnectionProfile, ...]:
        """Decode profile records under the exact fields of their stored schema."""

        if not isinstance(connections, list):
            raise ConnectionStoreError("Connection file must contain a connections list")
        if len(connections) > MAX_CONNECTION_CATALOG_ITEMS:
            raise ConnectionStoreError("Connection catalog contains too many profiles")

        profiles: list[ConnectionProfile] = []
        profile_ids: set[UUID] = set()
        expected_profile_fields = PROFILE_FIELDS_BY_SCHEMA[schema_version]
        for index, connection in enumerate(connections, start=1):
            try:
                if not isinstance(connection, dict):
                    raise ValueError("profile must be a JSON object")
                if set(connection) != expected_profile_fields:
                    raise ValueError(f"profile fields do not match schema version {schema_version}")
                if schema_version < 12 and connection["protocol"] not in ("ssh", "telnet"):
                    raise ValueError("Unsupported protocol in legacy connection schema")
                port = connection["port"]
                if isinstance(port, bool) or not isinstance(port, int):
                    raise ValueError("Connection port must be an integer")
                if schema_version >= 3 and connection["username"] is not None:
                    raise ValueError(
                        f"Connection username must be null in schema version {schema_version}"
                    )

                profile_id = cls._uuid_from_value(
                    connection["id"],
                    field_name="profile ID",
                )
                if profile_id in profile_ids:
                    raise ValueError("profile ID is duplicated")

                profile = ConnectionProfile(
                    profile_id=profile_id,
                    name=connection["name"],
                    protocol=ConnectionProtocol(connection["protocol"]),
                    host=connection["host"],
                    accent_color=None if schema_version < 15 else connection["accent_color"],
                    word_delete_mode=(WordDeleteMode.ESCAPE_DEL if schema_version < 14 else
                                      WordDeleteMode(connection["word_delete_mode"])),
                    rdp_options=(RdpOptions() if schema_version < 12 else
                                 rdp_options_from_document(connection["rdp_options"],
                                                           legacy=schema_version == 12)),
                    port=port,
                    telnet_encoding=(
                        TelnetEncoding.UTF8
                        if schema_version <= 2
                        else TelnetEncoding(connection["telnet_encoding"])
                    ),
                    # Accept legacy schema-v1/v2 usernames without allowing them to
                    # bypass the login chooser or terminal-owned ``login as`` flow.
                    username=None,
                    ssh_cipher=None if schema_version <= 3 else connection["ssh_cipher"],
                    ssh_algorithms=(SshAlgorithms() if schema_version < 10 else
                                    algorithms_from_document(connection["ssh_algorithms"], allow_ssh1=schema_version >= 11)),
                    ssh_account_id=(None if schema_version < 10 or
                                    connection["ssh_account_id"] is None else
                                    cls._uuid_from_value(connection["ssh_account_id"],
                                                         field_name="SSH account ID")),
                    ssh_options=(
                        SshConnectionOptions()
                        if schema_version <= 4
                        else ssh_options_from_document(connection["ssh_options"])
                    ),
                    is_favorite=False if schema_version <= 5 else connection["is_favorite"],
                    ssh_identity=(
                        SshIdentityOptions()
                        if schema_version <= 6
                        else ssh_identity_from_document(connection["ssh_identity"])
                    ),
                    ssh_jump=(
                        SshJumpOptions()
                        if schema_version <= 7
                        else ssh_jump_from_document(connection["ssh_jump"])
                    ),
                    ssh_tunnels=(
                        SshTunnelOptions()
                        if schema_version <= 8
                        else ssh_tunnels_from_document(connection["ssh_tunnels"])
                    ),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ConnectionStoreError(
                    f"Invalid connection profile #{index}: {error}"
                ) from error
            profiles.append(profile)
            profile_ids.add(profile.profile_id)

        return tuple(profiles)

    @staticmethod
    def _uuid_from_value(value: Any, *, field_name: str) -> UUID:
        """Decode one canonical lower-case UUID string used by the tree graph."""
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError(f"{field_name} must use canonical UUID format")
        return parsed
