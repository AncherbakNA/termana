"""Portable, credential-free import and export for connection catalogs."""

import json
from dataclasses import dataclass, replace
from typing import Any, Final
from uuid import UUID, uuid4

from termana.models.connection import (
    MAX_CONNECTION_CATALOG_ITEMS,
    MAX_CONNECTION_FOLDER_DEPTH,
    MAX_CONNECTION_FOLDER_NAME_CHARACTERS,
    MAX_CONNECTION_FOLDERS,
    ConnectionCatalog,
    ConnectionFolder,
    ConnectionProfile,
    ConnectionProtocol,
    TelnetEncoding,
    WordDeleteMode,
    connection_folder_name_key,
)
from termana.models.rdp_options import RdpOptions
from termana.models.ssh_algorithms import (
    SshAlgorithms,
    algorithms_from_document,
    algorithms_to_document,
)
from termana.models.ssh_jump import SshJumpOptions
from termana.models.ssh_options import SshConnectionOptions
from termana.storage.rdp_options_codec import rdp_options_from_document, rdp_options_to_document
from termana.storage.ssh_jump_codec import ssh_jump_from_document, ssh_jump_to_document
from termana.storage.ssh_options_codec import (
    ssh_options_from_document,
    ssh_options_to_document,
)

CONNECTION_TRANSFER_FORMAT: Final = "termana-connections"
CONNECTION_TRANSFER_VERSION: Final = 12
SUPPORTED_CONNECTION_TRANSFER_VERSIONS: Final[frozenset[int]] = frozenset(range(1, 13))
MAX_CONNECTION_TRANSFER_BYTES: Final = 4 * 1024 * 1024
MAX_UNIQUE_ID_GENERATION_ATTEMPTS: Final = 128

__all__ = (
    "CONNECTION_TRANSFER_FORMAT",
    "CONNECTION_TRANSFER_VERSION",
    "MAX_CONNECTION_TRANSFER_BYTES",
    "SUPPORTED_CONNECTION_TRANSFER_VERSIONS",
    "ConnectionTransferError",
    "ConnectionTransferMergeResult",
    "ConnectionTransferStats",
    "DecodedConnectionTransfer",
    "decode_connection_transfer",
    "encode_connection_catalog",
    "merge_connection_transfer",
)

_DOCUMENT_FIELDS: Final = frozenset({"format", "version", "items"})
_FOLDER_FIELDS: Final = frozenset({"type", "name", "items"})
_CONNECTION_FIELDS_V1: Final = frozenset({"type", "name", "protocol", "host", "port"})
_CONNECTION_FIELDS_V2: Final = _CONNECTION_FIELDS_V1 | {"telnet_encoding"}
_CONNECTION_FIELDS_V3: Final = _CONNECTION_FIELDS_V2 | {"ssh_cipher"}
_CONNECTION_FIELDS_V4: Final = _CONNECTION_FIELDS_V3 | {"ssh_options"}
_CONNECTION_FIELDS_V5: Final = _CONNECTION_FIELDS_V4 | {"is_favorite"}
_CONNECTION_FIELDS_V6: Final = _CONNECTION_FIELDS_V5 | {"ssh_jump"}
_CONNECTION_FIELDS_V7: Final = _CONNECTION_FIELDS_V6 | {"ssh_algorithms"}
_CONNECTION_FIELDS_V9: Final = _CONNECTION_FIELDS_V7 | {"rdp_options"}
_CONNECTION_FIELDS_V11: Final = _CONNECTION_FIELDS_V9 | {"word_delete_mode"}
_CONNECTION_FIELDS_V12: Final = _CONNECTION_FIELDS_V11 | {"accent_color"}
_FOLDER_ITEM_TYPE: Final = "folder"
_CONNECTION_ITEM_TYPE: Final = "connection"


class ConnectionTransferError(ValueError):
    """Raised when portable connection data cannot be encoded, decoded, or merged."""


@dataclass(frozen=True, slots=True)
class ConnectionTransferStats:
    """Credential-free counts suitable for import previews and completion messages."""

    ssh_connections: int
    telnet_connections: int
    folders: int
    renamed_folders: int = 0
    rdp_connections: int = 0

    def __post_init__(self) -> None:
        """Keep untrusted counts exact, non-negative, and within catalog bounds."""
        for field_name in (
            "ssh_connections",
            "telnet_connections",
            "folders",
            "renamed_folders",
            "rdp_connections",
        ):
            value = getattr(self, field_name)
            if type(value) is not int:
                raise TypeError(f"{field_name} must be an int")
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.folders > MAX_CONNECTION_FOLDERS:
            raise ValueError("folders exceeds the supported catalog limit")
        if self.total_items > MAX_CONNECTION_CATALOG_ITEMS:
            raise ValueError("connection transfer contains too many items")
        if self.renamed_folders > self.folders:
            raise ValueError("renamed_folders cannot exceed folders")

    @property
    def connections(self) -> int:
        """Return the total number of supported connection profiles."""
        return self.ssh_connections + self.telnet_connections + self.rdp_connections

    @property
    def total_items(self) -> int:
        """Return the number of folders and connection profiles together."""
        return self.connections + self.folders


@dataclass(frozen=True, slots=True)
class DecodedConnectionTransfer:
    """A validated portable catalog plus counts computed from that exact tree."""

    catalog: ConnectionCatalog
    stats: ConnectionTransferStats

    def __post_init__(self) -> None:
        """Prevent a caller from pairing a catalog with stale preview counts."""
        if not isinstance(self.catalog, ConnectionCatalog):
            raise TypeError("catalog must be a ConnectionCatalog")
        if not isinstance(self.stats, ConnectionTransferStats):
            raise TypeError("stats must be ConnectionTransferStats")
        if self.stats.renamed_folders:
            raise ValueError("decoded transfer stats cannot contain renamed folders")
        if self.stats != _stats_for_catalog(self.catalog):
            raise ValueError("decoded transfer stats do not match the catalog")


@dataclass(frozen=True, slots=True)
class ConnectionTransferMergeResult:
    """An immutable merged catalog and statistics for the imported portion."""

    catalog: ConnectionCatalog
    stats: ConnectionTransferStats

    def __post_init__(self) -> None:
        """Reject malformed values before they reach UI or persistence code."""
        if not isinstance(self.catalog, ConnectionCatalog):
            raise TypeError("catalog must be a ConnectionCatalog")
        if not isinstance(self.stats, ConnectionTransferStats):
            raise TypeError("stats must be ConnectionTransferStats")


class _DuplicateJsonKeyError(Exception):
    """Identify duplicate object members without conflating other JSON failures."""


class _InvalidJsonConstantError(Exception):
    """Identify non-standard NaN and infinity tokens rejected by the format."""


def _stats_for_catalog(
    catalog: ConnectionCatalog,
    *,
    renamed_folders: int = 0,
) -> ConnectionTransferStats:
    """Count supported records in one already-validated catalog."""
    return ConnectionTransferStats(
        ssh_connections=sum(
            profile.protocol is ConnectionProtocol.SSH for profile in catalog.profiles
        ),
        telnet_connections=sum(
            profile.protocol is ConnectionProtocol.TELNET for profile in catalog.profiles
        ),
        folders=len(catalog.folders),
        rdp_connections=sum(profile.protocol is ConnectionProtocol.RDP for profile in catalog.profiles),
        renamed_folders=renamed_folders,
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one object while rejecting duplicate members at every nesting level."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject JSON extensions such as NaN and Infinity deterministically."""
    raise _InvalidJsonConstantError(f"unsupported JSON numeric constant: {value}")


def _fresh_uuid(forbidden_ids: set[UUID]) -> UUID:
    """Generate one UUID that cannot inherit identity from either source catalog."""
    for _attempt in range(MAX_UNIQUE_ID_GENERATION_ATTEMPTS):
        candidate = uuid4()
        if not isinstance(candidate, UUID):
            raise ConnectionTransferError("UUID generator returned an unsupported value")
        if candidate in forbidden_ids:
            continue
        forbidden_ids.add(candidate)
        return candidate
    raise ConnectionTransferError("Could not generate a unique connection item ID")


def _connection_item_document(profile: ConnectionProfile) -> dict[str, object]:
    """Exclude credentials, IDs, local SSH identity choices, and tunnel rules."""
    return {
        "type": _CONNECTION_ITEM_TYPE,
        "name": profile.name,
        "protocol": profile.protocol.value,
        "host": profile.host,
        "port": profile.port,
        "telnet_encoding": profile.telnet_encoding.value,
        "ssh_cipher": profile.ssh_cipher,
        "ssh_options": ssh_options_to_document(profile.ssh_options),
        "is_favorite": profile.is_favorite,
        "ssh_jump": ssh_jump_to_document(profile.ssh_jump),
        "ssh_algorithms": algorithms_to_document(profile.ssh_algorithms),
        "rdp_options": rdp_options_to_document(profile.rdp_options, portable=True),
        "word_delete_mode": profile.word_delete_mode.value,
        "accent_color": profile.accent_color,
    }


def _catalog_items_document(
    catalog: ConnectionCatalog,
    parent_folder_id: UUID | None,
) -> list[dict[str, object]]:
    """Encode one ordered sibling list into the nested portable representation."""
    items: list[dict[str, object]] = []
    for item in catalog.children_of(parent_folder_id):
        if isinstance(item, ConnectionProfile):
            items.append(_connection_item_document(item))
            continue
        items.append(
            {
                "type": _FOLDER_ITEM_TYPE,
                "name": item.name,
                "items": _catalog_items_document(catalog, item.folder_id),
            }
        )
    return items


def encode_connection_catalog(catalog: ConnectionCatalog) -> bytes:
    """Return deterministic UTF-8 JSON containing no UUID, login, or credential data."""
    if not isinstance(catalog, ConnectionCatalog):
        raise TypeError("catalog must be a ConnectionCatalog")
    document = {
        "format": CONNECTION_TRANSFER_FORMAT,
        "version": CONNECTION_TRANSFER_VERSION,
        "items": _catalog_items_document(catalog, None),
    }
    try:
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ConnectionTransferError(f"Could not encode connection transfer: {error}") from error
    if len(payload) > MAX_CONNECTION_TRANSFER_BYTES:
        raise ConnectionTransferError("Connection transfer exceeds the supported size limit")
    return payload


class _ConnectionTransferDecoder:
    """Build one fresh-ID catalog while enforcing structural limits incrementally."""

    def __init__(self, *, version: int) -> None:
        if type(version) is not int or version not in SUPPORTED_CONNECTION_TRANSFER_VERSIONS:
            raise ValueError("Unsupported connection transfer decoder version")
        self.version = version
        self._used_ids: set[UUID] = set()
        self.folders: list[ConnectionFolder] = []
        self.profiles: list[ConnectionProfile] = []
        self.total_items = 0
        self.folder_count = 0

    def decode_items(
        self,
        value: object,
        *,
        parent_folder_depth: int,
        path: str,
    ) -> tuple[UUID, ...]:
        """Decode one exact JSON array without losing mixed sibling order."""
        if type(value) is not list:
            raise ConnectionTransferError(f"{path} must be a JSON array")
        if len(value) > MAX_CONNECTION_CATALOG_ITEMS - self.total_items:
            raise ConnectionTransferError("Connection transfer contains too many items")
        child_ids: list[UUID] = []
        for index, item in enumerate(value, start=1):
            child_ids.append(
                self.decode_item(
                    item,
                    parent_folder_depth=parent_folder_depth,
                    path=f"{path}[{index}]",
                )
            )
        return tuple(child_ids)

    def decode_item(
        self,
        value: object,
        *,
        parent_folder_depth: int,
        path: str,
    ) -> UUID:
        """Decode one folder or profile through the canonical model validators."""
        if type(value) is not dict:
            raise ConnectionTransferError(f"{path} must be a JSON object")
        self.total_items += 1
        if self.total_items > MAX_CONNECTION_CATALOG_ITEMS:
            raise ConnectionTransferError("Connection transfer contains too many items")

        item_type = value.get("type")
        if item_type == _FOLDER_ITEM_TYPE:
            return self._decode_folder(
                value,
                parent_folder_depth=parent_folder_depth,
                path=path,
            )
        if item_type == _CONNECTION_ITEM_TYPE:
            return self._decode_connection(value, path=path)
        raise ConnectionTransferError(f"{path} has an unsupported item type")

    def _decode_folder(
        self,
        value: dict[str, Any],
        *,
        parent_folder_depth: int,
        path: str,
    ) -> UUID:
        """Decode a bounded folder before recursively attaching its children."""
        if set(value) != _FOLDER_FIELDS:
            raise ConnectionTransferError(
                f"{path} folder fields do not match version {self.version}"
            )
        folder_depth = parent_folder_depth + 1
        if folder_depth > MAX_CONNECTION_FOLDER_DEPTH:
            raise ConnectionTransferError("Connection folder nesting exceeds the supported depth")
        self.folder_count += 1
        if self.folder_count > MAX_CONNECTION_FOLDERS:
            raise ConnectionTransferError("Connection transfer contains too many folders")

        folder_id = _fresh_uuid(self._used_ids)
        try:
            folder = ConnectionFolder(
                name=value.get("name"),
                folder_id=folder_id,
            )
        except (TypeError, ValueError) as error:
            raise ConnectionTransferError(f"Invalid {path}: {error}") from error

        # Reserve preorder placement before descendants are decoded.  This keeps
        # the normalized folder tuple deterministic without exposing IDs on disk.
        folder_index = len(self.folders)
        self.folders.append(folder)
        child_ids = self.decode_items(
            value.get("items"),
            parent_folder_depth=folder_depth,
            path=f"{path}.items",
        )
        try:
            self.folders[folder_index] = replace(folder, child_ids=child_ids)
        except (TypeError, ValueError) as error:
            raise ConnectionTransferError(f"Invalid {path}: {error}") from error
        return folder_id

    def _decode_connection(self, value: dict[str, Any], *, path: str) -> UUID:
        """Decode one credential-free versioned endpoint."""
        expected_fields = {
            1: _CONNECTION_FIELDS_V1,
            2: _CONNECTION_FIELDS_V2,
            3: _CONNECTION_FIELDS_V3,
            4: _CONNECTION_FIELDS_V4,
            5: _CONNECTION_FIELDS_V5,
            6: _CONNECTION_FIELDS_V6,
            7: _CONNECTION_FIELDS_V7,
            8: _CONNECTION_FIELDS_V7,
            9: _CONNECTION_FIELDS_V9,
            10: _CONNECTION_FIELDS_V9,
            11: _CONNECTION_FIELDS_V11,
            12: _CONNECTION_FIELDS_V12,
        }[self.version]
        if set(value) != expected_fields:
            raise ConnectionTransferError(
                f"{path} connection fields do not match version {self.version}"
            )
        port = value.get("port")
        if type(port) is not int:
            raise ConnectionTransferError(f"Invalid {path}: Connection port must be an integer")
        try:
            if self.version < 9 and value.get("protocol") not in ("ssh", "telnet"):
                raise ValueError("Unsupported protocol in legacy portable format")
            protocol = ConnectionProtocol(value.get("protocol"))
            profile = ConnectionProfile(
                profile_id=_fresh_uuid(self._used_ids),
                name=value.get("name"),
                protocol=protocol,
                host=value.get("host"),
                accent_color=None if self.version < 12 else value["accent_color"],
                word_delete_mode=(WordDeleteMode.ESCAPE_DEL if self.version < 11 else
                                  WordDeleteMode(value["word_delete_mode"])),
                port=port,
                rdp_options=(RdpOptions() if self.version < 9 else
                             rdp_options_from_document(value["rdp_options"], portable=True,
                                                       legacy=self.version == 9)),
                telnet_encoding=(
                    TelnetEncoding.UTF8
                    if self.version == 1
                    else TelnetEncoding(value.get("telnet_encoding"))
                ),
                username=None,
                ssh_cipher=None if self.version <= 2 else value["ssh_cipher"],
                ssh_algorithms=(SshAlgorithms() if self.version < 7 else
                                algorithms_from_document(value["ssh_algorithms"], allow_ssh1=self.version >= 8)),
                ssh_options=(
                    SshConnectionOptions()
                    if self.version <= 3
                    else ssh_options_from_document(value["ssh_options"])
                ),
                is_favorite=False if self.version <= 4 else value["is_favorite"],
                ssh_jump=(
                    SshJumpOptions()
                    if self.version <= 5
                    else ssh_jump_from_document(value["ssh_jump"])
                ),
            )
        except (TypeError, ValueError) as error:
            raise ConnectionTransferError(f"Invalid {path}: {error}") from error
        self.profiles.append(profile)
        return profile.profile_id


def decode_connection_transfer(payload: bytes) -> DecodedConnectionTransfer:
    """Strictly decode one bounded portable document into a fresh-ID catalog."""
    if type(payload) is not bytes:
        raise TypeError("payload must be bytes")
    if len(payload) > MAX_CONNECTION_TRANSFER_BYTES:
        raise ConnectionTransferError("Connection transfer exceeds the supported size limit")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise ConnectionTransferError("Connection transfer must be valid UTF-8") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJsonKeyError as error:
        raise ConnectionTransferError(str(error)) from error
    except _InvalidJsonConstantError as error:
        raise ConnectionTransferError(str(error)) from error
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ConnectionTransferError(f"Connection transfer is not valid JSON: {error}") from error

    if type(document) is not dict:
        raise ConnectionTransferError("Connection transfer must contain a JSON object")
    if set(document) != _DOCUMENT_FIELDS:
        raise ConnectionTransferError("Connection transfer fields do not match a supported version")
    if document.get("format") != CONNECTION_TRANSFER_FORMAT:
        raise ConnectionTransferError("Unsupported connection transfer format")
    version = document.get("version")
    if type(version) is not int or version not in SUPPORTED_CONNECTION_TRANSFER_VERSIONS:
        raise ConnectionTransferError("Unsupported connection transfer version")

    decoder = _ConnectionTransferDecoder(version=version)
    root_item_ids = decoder.decode_items(
        document.get("items"),
        parent_folder_depth=0,
        path="items",
    )
    try:
        catalog = ConnectionCatalog(
            root_item_ids=root_item_ids,
            folders=tuple(decoder.folders),
            profiles=tuple(decoder.profiles),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ConnectionTransferError(f"Invalid connection transfer catalog: {error}") from error
    stats = _stats_for_catalog(catalog)
    return DecodedConnectionTransfer(catalog=catalog, stats=stats)


def _all_catalog_item_ids(catalog: ConnectionCatalog) -> set[UUID]:
    """Return both globally shared ID namespaces from one immutable catalog."""
    return {
        *(folder.folder_id for folder in catalog.folders),
        *(profile.profile_id for profile in catalog.profiles),
    }


def _available_imported_root_folder_name(
    name: str,
    reserved_name_keys: set[str],
) -> tuple[str, bool]:
    """Resolve one root conflict with a bounded, model-valid deterministic suffix."""
    if connection_folder_name_key(name) not in reserved_name_keys:
        return name, False

    imported_number = 1
    while True:
        suffix = " (imported)" if imported_number == 1 else f" (imported {imported_number})"
        prefix = name[: MAX_CONNECTION_FOLDER_NAME_CHARACTERS - len(suffix)].rstrip()
        candidate = f"{prefix}{suffix}"
        if connection_folder_name_key(candidate) not in reserved_name_keys:
            return candidate, True
        imported_number += 1


def merge_connection_transfer(
    existing: ConnectionCatalog,
    decoded: DecodedConnectionTransfer,
) -> ConnectionTransferMergeResult:
    """Append imported roots with fresh identity and deterministic folder renames."""
    if not isinstance(existing, ConnectionCatalog):
        raise TypeError("existing must be a ConnectionCatalog")
    if not isinstance(decoded, DecodedConnectionTransfer):
        raise TypeError("decoded must be DecodedConnectionTransfer")

    imported = decoded.catalog
    imported_stats = _stats_for_catalog(imported)
    if len(existing.folders) + imported_stats.folders > MAX_CONNECTION_FOLDERS:
        raise ConnectionTransferError("Merged connection catalog would contain too many folders")
    if (
        len(existing.folders) + len(existing.profiles) + imported_stats.total_items
        > MAX_CONNECTION_CATALOG_ITEMS
    ):
        raise ConnectionTransferError("Merged connection catalog would contain too many items")

    forbidden_ids = _all_catalog_item_ids(existing) | _all_catalog_item_ids(imported)
    folders = list(existing.folders)
    profiles = list(existing.profiles)
    imported_root_ids: list[UUID] = []
    reserved_root_folder_name_keys = {
        connection_folder_name_key(item.name)
        for item in existing.children_of(None)
        if isinstance(item, ConnectionFolder)
    }
    renamed_folders = 0

    def clone_item(item: ConnectionFolder | ConnectionProfile, *, is_root: bool) -> UUID:
        nonlocal renamed_folders
        if isinstance(item, ConnectionProfile):
            cloned_profile = ConnectionProfile(
                profile_id=_fresh_uuid(forbidden_ids),
                name=item.name,
                protocol=item.protocol,
                host=item.host,
                port=item.port,
                telnet_encoding=item.telnet_encoding,
                word_delete_mode=item.word_delete_mode,
                accent_color=item.accent_color,
                ssh_cipher=item.ssh_cipher,
                ssh_algorithms=item.ssh_algorithms,
                ssh_options=item.ssh_options,
                is_favorite=item.is_favorite,
                ssh_jump=item.ssh_jump,
                rdp_options=item.rdp_options.without_login(),
                # Local SSH identity choices and tunnel rules intentionally use
                # defaults, even for an in-memory decoded catalog supplied here.
                username=None,
            )
            profiles.append(cloned_profile)
            return cloned_profile.profile_id

        folder_name = item.name
        if is_root:
            folder_name, renamed = _available_imported_root_folder_name(
                folder_name,
                reserved_root_folder_name_keys,
            )
            renamed_folders += int(renamed)
            reserved_root_folder_name_keys.add(connection_folder_name_key(folder_name))

        folder = ConnectionFolder(
            name=folder_name,
            folder_id=_fresh_uuid(forbidden_ids),
        )
        folder_index = len(folders)
        folders.append(folder)
        child_ids = tuple(
            clone_item(child, is_root=False) for child in imported.children_of(item.folder_id)
        )
        folders[folder_index] = replace(folder, child_ids=child_ids)
        return folder.folder_id

    for root_item in imported.children_of(None):
        imported_root_ids.append(clone_item(root_item, is_root=True))

    try:
        catalog = ConnectionCatalog(
            root_item_ids=(*existing.root_item_ids, *imported_root_ids),
            folders=tuple(folders),
            profiles=tuple(profiles),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ConnectionTransferError(f"Could not merge connection transfer: {error}") from error
    return ConnectionTransferMergeResult(
        catalog=catalog,
        stats=replace(imported_stats, renamed_folders=renamed_folders),
    )
