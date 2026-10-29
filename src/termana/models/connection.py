"""Connection profile data."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID, uuid4

from termana.models.rdp_options import DEFAULT_RDP_OPTIONS, RdpOptions
from termana.models.ssh_algorithms import DEFAULT_SSH_ALGORITHMS, SshAlgorithms
from termana.models.ssh_cipher import validate_ssh_cipher
from termana.models.ssh_identity import SshIdentityOptions
from termana.models.ssh_jump import SshJumpMode, SshJumpOptions, validate_ssh_route_host
from termana.models.ssh_options import SshConnectionOptions
from termana.models.ssh_tunnels import SshTunnelOptions

_DEFAULT_SSH_OPTIONS: Final = SshConnectionOptions()
_DEFAULT_SSH_IDENTITY: Final = SshIdentityOptions()
_DEFAULT_SSH_JUMP: Final = SshJumpOptions()
_DEFAULT_SSH_TUNNELS: Final = SshTunnelOptions()
MIN_NETWORK_PORT: Final = 1
MAX_NETWORK_PORT: Final = 65535
DEFAULT_SSH_PORT: Final = 22
DEFAULT_TELNET_PORT: Final = 23
DEFAULT_RDP_PORT: Final = 3389
MAX_CONNECTION_NAME_CHARACTERS: Final = 128
MAX_CONNECTION_HOST_CHARACTERS: Final = 253
MAX_CONNECTION_USERNAME_CHARACTERS: Final = 128
MAX_CONNECTION_FOLDER_NAME_CHARACTERS: Final = 128
MAX_CONNECTION_FOLDER_DEPTH: Final = 16
MAX_CONNECTION_FOLDERS: Final = 2_048
MAX_CONNECTION_CATALOG_ITEMS: Final = 10_000
_DIRECTIONAL_FORMATS: Final = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)
_CONNECTION_COPY_SUFFIX: Final = re.compile(
    r"^(?P<base>.+) (?i:copy)(?: (?P<number>[1-9][0-9]*))?$",
    re.DOTALL,
)


class ConnectionProtocol(StrEnum):
    """Protocols supported by connection profiles."""

    SSH = "ssh"
    TELNET = "telnet"
    RDP = "rdp"


class TelnetEncoding(StrEnum):
    """Stable, explicitly supported character encodings for Telnet sessions."""

    UTF8 = "utf-8"
    WINDOWS_1251 = "cp1251"
    DOS_866 = "cp866"
    KOI8_R = "koi8-r"
    LATIN_1 = "latin-1"


class WordDeleteMode(StrEnum):
    """Explicit Mac backward-word key mapping for one terminal connection."""

    ESCAPE_DEL = "escape-del"
    CTRL_W = "ctrl-w"


_DEFAULT_PORTS_BY_PROTOCOL: Final[Mapping[ConnectionProtocol, int]] = MappingProxyType(
    {
        ConnectionProtocol.SSH: DEFAULT_SSH_PORT,
        ConnectionProtocol.TELNET: DEFAULT_TELNET_PORT,
        ConnectionProtocol.RDP: DEFAULT_RDP_PORT,
    }
)


def default_port_for_protocol(protocol: ConnectionProtocol) -> int:
    """Return the conventional port for one explicitly supported protocol."""
    if not isinstance(protocol, ConnectionProtocol):
        raise ValueError("Unsupported connection protocol")
    return _DEFAULT_PORTS_BY_PROTOCOL[protocol]


@dataclass(frozen=True, slots=True, init=False)
class ConnectionProfile:
    """Settings needed to identify a remote connection."""

    name: str
    host: str
    protocol: ConnectionProtocol = ConnectionProtocol.SSH
    port: int = DEFAULT_SSH_PORT
    telnet_encoding: TelnetEncoding = TelnetEncoding.UTF8
    username: str | None = None
    profile_id: UUID = field(default_factory=uuid4)
    ssh_cipher: str | None = None
    ssh_options: SshConnectionOptions = _DEFAULT_SSH_OPTIONS
    is_favorite: bool = False
    ssh_identity: SshIdentityOptions = _DEFAULT_SSH_IDENTITY
    ssh_jump: SshJumpOptions = _DEFAULT_SSH_JUMP
    ssh_tunnels: SshTunnelOptions = _DEFAULT_SSH_TUNNELS
    ssh_algorithms: SshAlgorithms = DEFAULT_SSH_ALGORITHMS
    ssh_account_id: UUID | None = None
    rdp_options: RdpOptions = DEFAULT_RDP_OPTIONS
    word_delete_mode: WordDeleteMode = WordDeleteMode.ESCAPE_DEL
    accent_color: str | None = None

    def __init__(
        self,
        name: str,
        host: str,
        protocol: ConnectionProtocol = ConnectionProtocol.SSH,
        port: int | None = None,
        username: str | None = None,
        profile_id: UUID | None = None,
        telnet_encoding: TelnetEncoding = TelnetEncoding.UTF8,
        ssh_cipher: str | None = None,
        ssh_options: SshConnectionOptions = _DEFAULT_SSH_OPTIONS,
        is_favorite: bool = False,
        ssh_identity: SshIdentityOptions = _DEFAULT_SSH_IDENTITY,
        ssh_jump: SshJumpOptions = _DEFAULT_SSH_JUMP,
        ssh_tunnels: SshTunnelOptions = _DEFAULT_SSH_TUNNELS,
        ssh_algorithms: SshAlgorithms = DEFAULT_SSH_ALGORITHMS,
        ssh_account_id: UUID | None = None,
        rdp_options: RdpOptions = DEFAULT_RDP_OPTIONS,
        word_delete_mode: WordDeleteMode = WordDeleteMode.ESCAPE_DEL,
        accent_color: str | None = None,
    ) -> None:
        """Resolve a protocol-aware default while retaining an integer public port."""
        resolved_port = (
            default_port_for_protocol(protocol)
            if port is None and isinstance(protocol, ConnectionProtocol)
            else port
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "port", resolved_port)
        object.__setattr__(self, "telnet_encoding", telnet_encoding)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "ssh_cipher", ssh_cipher)
        object.__setattr__(self, "ssh_options", ssh_options)
        object.__setattr__(self, "is_favorite", is_favorite)
        object.__setattr__(self, "ssh_identity", ssh_identity)
        object.__setattr__(self, "ssh_jump", ssh_jump)
        object.__setattr__(self, "ssh_tunnels", ssh_tunnels)
        object.__setattr__(self, "ssh_algorithms", ssh_algorithms)
        object.__setattr__(self, "ssh_account_id", ssh_account_id)
        object.__setattr__(self, "rdp_options", rdp_options)
        object.__setattr__(self, "word_delete_mode", word_delete_mode)
        object.__setattr__(self, "accent_color", accent_color.upper() if isinstance(accent_color, str) else accent_color)
        object.__setattr__(
            self,
            "profile_id",
            uuid4() if profile_id is None else profile_id,
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        """Reject values that cannot describe a usable connection."""
        _validate_profile_text(
            self.name,
            field_name="Connection name",
            maximum_length=MAX_CONNECTION_NAME_CHARACTERS,
        )
        _validate_profile_text(
            self.host,
            field_name="Connection host",
            maximum_length=MAX_CONNECTION_HOST_CHARACTERS,
            forbid_whitespace=True,
        )
        if self.host.startswith("-"):
            raise ValueError("Connection host cannot begin with a hyphen")
        if "@" in self.host:
            raise ValueError("Connection host cannot contain @")
        if not isinstance(self.protocol, ConnectionProtocol):
            raise ValueError("Unsupported connection protocol")
        if self.accent_color is not None and (
            type(self.accent_color) is not str
            or re.fullmatch(r"#[0-9A-F]{6}", self.accent_color) is None
        ):
            raise ValueError("Connection color must be #RRGGBB or None")
        if not isinstance(self.word_delete_mode, WordDeleteMode):
            raise ValueError("Unsupported word deletion mode")
        if self.protocol is ConnectionProtocol.RDP and self.word_delete_mode is not WordDeleteMode.ESCAPE_DEL:
            raise ValueError("RDP profiles cannot select terminal word deletion")
        if type(self.rdp_options) is not RdpOptions:
            raise ValueError("RDP options must be RdpOptions")
        if self.protocol is not ConnectionProtocol.RDP and not self.rdp_options.is_default:
            raise ValueError("Only RDP profiles can select RDP options")
        if self.protocol is ConnectionProtocol.RDP:
            if self.username is not None:
                raise ValueError("RDP login hints belong in RDP options, not legacy username")
            if len(self.host.encode("utf-8")) > 255:
                raise ValueError("Connection host exceeds the RDP byte limit")
        if type(self.is_favorite) is not bool:
            raise ValueError("Connection favorite flag must be a bool")
        validate_ssh_cipher(self.ssh_cipher)
        if type(self.ssh_algorithms) is not SshAlgorithms:
            raise ValueError("SSH algorithms must be SshAlgorithms")
        if self.ssh_account_id is not None and not isinstance(self.ssh_account_id, UUID):
            raise ValueError("SSH account ID must be a UUID")
        if self.protocol is ConnectionProtocol.TELNET and (
            not self.ssh_algorithms.is_default or self.ssh_account_id is not None
        ):
            raise ValueError("Telnet cannot select SSH algorithms or accounts")
        if self.protocol is ConnectionProtocol.TELNET and self.ssh_cipher is not None:
            raise ValueError("Telnet connection profiles cannot select an SSH cipher")
        if type(self.ssh_options) is not SshConnectionOptions:
            raise ValueError("SSH options must be SshConnectionOptions")
        if self.protocol is ConnectionProtocol.TELNET and not self.ssh_options.is_default:
            raise ValueError("Telnet connection profiles cannot select SSH options")
        if type(self.ssh_identity) is not SshIdentityOptions:
            raise ValueError("SSH identity must be SshIdentityOptions")
        if self.protocol is ConnectionProtocol.TELNET and not self.ssh_identity.is_default:
            raise ValueError("Telnet connection profiles cannot select an SSH identity")
        if type(self.ssh_jump) is not SshJumpOptions:
            raise ValueError("SSH jump options must be SshJumpOptions")
        if self.protocol is ConnectionProtocol.TELNET and not self.ssh_jump.is_default:
            raise ValueError("Telnet connection profiles cannot select an SSH jump route")
        if type(self.ssh_tunnels) is not SshTunnelOptions:
            raise ValueError("SSH tunnels must be SshTunnelOptions")
        if self.protocol is ConnectionProtocol.TELNET and not self.ssh_tunnels.is_default:
            raise ValueError("Telnet connection profiles cannot select SSH tunnels")
        if self.protocol is ConnectionProtocol.RDP and (
            self.ssh_cipher is not None or self.ssh_account_id is not None
            or not self.ssh_algorithms.is_default or not self.ssh_options.is_default
            or not self.ssh_identity.is_default or not self.ssh_jump.is_default
            or not self.ssh_tunnels.is_default
        ):
            raise ValueError("RDP profiles cannot select SSH settings or accounts")
        if self.ssh_jump.mode is SshJumpMode.JUMP:
            validate_ssh_route_host(self.host)
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("Connection port must be an integer")
        if not MIN_NETWORK_PORT <= self.port <= MAX_NETWORK_PORT:
            raise ValueError(
                f"Connection port must be between {MIN_NETWORK_PORT} and {MAX_NETWORK_PORT}"
            )
        if not isinstance(self.telnet_encoding, TelnetEncoding):
            raise ValueError("Unsupported Telnet encoding")
        if (
            self.protocol is not ConnectionProtocol.TELNET
            and self.telnet_encoding is not TelnetEncoding.UTF8
        ):
            raise ValueError(f"{self.protocol.upper()} connection profiles cannot select a Telnet encoding")
        if self.username is not None:
            _validate_profile_text(
                self.username,
                field_name="Username",
                maximum_length=MAX_CONNECTION_USERNAME_CHARACTERS,
                forbid_whitespace=True,
            )
        if not isinstance(self.profile_id, UUID):
            raise ValueError("Connection profile ID must be a UUID")


@dataclass(frozen=True, slots=True)
class ConnectionFolder:
    """One immutable folder whose child IDs define their visible order."""

    name: str
    folder_id: UUID = field(default_factory=uuid4)
    child_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        """Validate a portable label and an ordered sequence of stable IDs."""
        _validate_profile_text(
            self.name,
            field_name="Connection folder name",
            maximum_length=MAX_CONNECTION_FOLDER_NAME_CHARACTERS,
        )
        if "/" in self.name:
            raise ValueError("Connection folder name cannot contain /")
        if not isinstance(self.folder_id, UUID):
            raise ValueError("Connection folder ID must be a UUID")
        if type(self.child_ids) is not tuple:
            raise ValueError("Connection folder child IDs must be a tuple")
        if any(not isinstance(child_id, UUID) for child_id in self.child_ids):
            raise ValueError("Connection folder child IDs must be UUIDs")
        if len(self.child_ids) > MAX_CONNECTION_CATALOG_ITEMS:
            raise ValueError("Connection folder contains too many children")
        if len(self.child_ids) != len(set(self.child_ids)):
            raise ValueError("Connection folder child IDs must be unique")


@dataclass(frozen=True, slots=True)
class ConnectionCatalog:
    """A complete immutable tree of saved folders and connection profiles."""

    root_item_ids: tuple[UUID, ...] = ()
    folders: tuple[ConnectionFolder, ...] = ()
    profiles: tuple[ConnectionProfile, ...] = ()
    _items_by_id: Mapping[UUID, ConnectionFolder | ConnectionProfile] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )
    _parent_ids_by_item_id: Mapping[UUID, UUID | None] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        """Reject ambiguous, cyclic, unreachable, or excessively large trees."""
        if type(self.root_item_ids) is not tuple:
            raise ValueError("Connection catalog root item IDs must be a tuple")
        if type(self.folders) is not tuple:
            raise ValueError("Connection catalog folders must be a tuple")
        if type(self.profiles) is not tuple:
            raise ValueError("Connection catalog profiles must be a tuple")
        if any(not isinstance(item_id, UUID) for item_id in self.root_item_ids):
            raise ValueError("Connection catalog root item IDs must be UUIDs")
        if any(not isinstance(folder, ConnectionFolder) for folder in self.folders):
            raise ValueError("Connection catalog folders contain an unsupported value")
        if any(not isinstance(profile, ConnectionProfile) for profile in self.profiles):
            raise ValueError("Connection catalog profiles contain an unsupported value")
        if len(self.folders) > MAX_CONNECTION_FOLDERS:
            raise ValueError("Connection catalog contains too many folders")
        if len(self.folders) + len(self.profiles) > MAX_CONNECTION_CATALOG_ITEMS:
            raise ValueError("Connection catalog contains too many items")
        placement_count = len(self.root_item_ids) + sum(
            len(folder.child_ids) for folder in self.folders
        )
        if placement_count > MAX_CONNECTION_CATALOG_ITEMS:
            raise ValueError("Connection catalog contains too many item placements")

        folder_ids = [folder.folder_id for folder in self.folders]
        profile_ids = [profile.profile_id for profile in self.profiles]
        all_item_ids = [*folder_ids, *profile_ids]
        if len(all_item_ids) != len(set(all_item_ids)):
            raise ValueError("Connection catalog item IDs must be globally unique")

        placed_item_ids = [
            *self.root_item_ids,
            *(child_id for folder in self.folders for child_id in folder.child_ids),
        ]
        if len(placed_item_ids) != len(set(placed_item_ids)):
            raise ValueError("Every connection catalog item must be placed exactly once")
        if set(placed_item_ids) != set(all_item_ids):
            raise ValueError("Connection catalog contains unknown or unreachable items")

        folders_by_id = {folder.folder_id: folder for folder in self.folders}
        profiles_by_id = {profile.profile_id: profile for profile in self.profiles}

        def validate_unique_sibling_folder_names(child_ids: tuple[UUID, ...]) -> None:
            sibling_name_keys: set[str] = set()
            for child_id in child_ids:
                child_folder = folders_by_id.get(child_id)
                if child_folder is None:
                    continue
                name_key = connection_folder_name_key(child_folder.name)
                if name_key in sibling_name_keys:
                    raise ValueError("Connection folder names must be unique among siblings")
                sibling_name_keys.add(name_key)

        validate_unique_sibling_folder_names(self.root_item_ids)
        for folder in self.folders:
            validate_unique_sibling_folder_names(folder.child_ids)

        visited: set[UUID] = set()

        def visit(item_id: UUID, folder_depth: int) -> None:
            if item_id in visited:
                raise ValueError("Connection catalog contains a folder cycle")
            visited.add(item_id)
            folder = folders_by_id.get(item_id)
            if folder is None:
                return
            if folder_depth > MAX_CONNECTION_FOLDER_DEPTH:
                raise ValueError("Connection folder nesting exceeds the supported depth")
            for child_id in folder.child_ids:
                visit(child_id, folder_depth + 1)

        for root_item_id in self.root_item_ids:
            visit(root_item_id, 1)
        if visited != set(all_item_ids):
            raise ValueError("Connection catalog contains unreachable or cyclic items")

        items_by_id: dict[UUID, ConnectionFolder | ConnectionProfile] = {
            **folders_by_id,
            **profiles_by_id,
        }
        parent_ids_by_item_id: dict[UUID, UUID | None] = {
            item_id: None for item_id in self.root_item_ids
        }
        for folder in self.folders:
            parent_ids_by_item_id.update(
                {child_id: folder.folder_id for child_id in folder.child_ids}
            )
        object.__setattr__(self, "_items_by_id", MappingProxyType(items_by_id))
        object.__setattr__(
            self,
            "_parent_ids_by_item_id",
            MappingProxyType(parent_ids_by_item_id),
        )

    def children_of(
        self,
        parent_folder_id: UUID | None,
    ) -> tuple[ConnectionFolder | ConnectionProfile, ...]:
        """Return one parent's mixed children in their persisted order."""
        child_ids = self._child_ids_of(parent_folder_id)
        return tuple(self._items_by_id[child_id] for child_id in child_ids)

    def profile_by_id(self, profile_id: UUID) -> ConnectionProfile | None:
        """Find a profile by stable identity without depending on its placement."""
        item = self._items_by_id.get(profile_id)
        return item if isinstance(item, ConnectionProfile) else None

    def folder_by_id(self, folder_id: UUID) -> ConnectionFolder | None:
        """Find a folder by stable identity without walking its descendants."""
        item = self._items_by_id.get(folder_id)
        return item if isinstance(item, ConnectionFolder) else None

    def parent_id_of(self, item_id: UUID) -> UUID | None:
        """Return the owning folder, using None only for a real root item."""
        self._require_item(item_id)
        return self._parent_ids_by_item_id[item_id]

    def folder_choices(self) -> tuple[tuple[UUID, str], ...]:
        """Return stable folder IDs and readable paths in depth-first order."""
        choices: list[tuple[UUID, str]] = []

        def append_folder(folder: ConnectionFolder, parent_path: str) -> None:
            path = f"{parent_path} / {folder.name}" if parent_path else folder.name
            choices.append((folder.folder_id, path))
            for child in self.children_of(folder.folder_id):
                if isinstance(child, ConnectionFolder):
                    append_folder(child, path)

        for root_item in self.children_of(None):
            if isinstance(root_item, ConnectionFolder):
                append_folder(root_item, "")
        return tuple(choices)

    def with_profile_added(
        self,
        profile: ConnectionProfile,
        *,
        parent_folder_id: UUID | None = None,
        index: int | None = None,
    ) -> ConnectionCatalog:
        """Return a snapshot with one new profile inserted under a parent."""
        if not isinstance(profile, ConnectionProfile):
            raise ValueError("Expected a ConnectionProfile")
        if self._item_exists(profile.profile_id):
            raise ValueError("Connection catalog item ID already exists")
        child_ids = self._insert_child(
            self._child_ids_of(parent_folder_id),
            profile.profile_id,
            index,
        )
        return self._with_parent_children(
            parent_folder_id,
            child_ids,
            profiles=(*self.profiles, profile),
        )

    def with_profile_updated(self, profile: ConnectionProfile) -> ConnectionCatalog:
        """Replace profile fields while preserving its ID and tree placement."""
        if not isinstance(profile, ConnectionProfile):
            raise ValueError("Expected a ConnectionProfile")
        profile_index = next(
            (
                index
                for index, current in enumerate(self.profiles)
                if current.profile_id == profile.profile_id
            ),
            None,
        )
        if profile_index is None:
            raise ValueError("Connection profile does not exist")
        profiles = list(self.profiles)
        profiles[profile_index] = profile
        return replace(self, profiles=tuple(profiles))

    def without_profile(self, profile_id: UUID) -> ConnectionCatalog:
        """Return a snapshot without one saved profile or its placement."""
        profile = self.profile_by_id(profile_id)
        if profile is None:
            raise ValueError("Connection profile does not exist")
        parent_folder_id = self.parent_id_of(profile_id)
        child_ids = tuple(
            child_id for child_id in self._child_ids_of(parent_folder_id) if child_id != profile_id
        )
        profiles = tuple(current for current in self.profiles if current.profile_id != profile_id)
        return self._with_parent_children(
            parent_folder_id,
            child_ids,
            profiles=profiles,
        )

    def with_folder_added(
        self,
        folder: ConnectionFolder,
        *,
        parent_folder_id: UUID | None = None,
        index: int | None = None,
    ) -> ConnectionCatalog:
        """Return a snapshot with one new empty folder inserted under a parent."""
        if not isinstance(folder, ConnectionFolder):
            raise ValueError("Expected a ConnectionFolder")
        if folder.child_ids:
            raise ValueError("A newly added connection folder must be empty")
        if self._item_exists(folder.folder_id):
            raise ValueError("Connection catalog item ID already exists")
        child_ids = self._insert_child(
            self._child_ids_of(parent_folder_id),
            folder.folder_id,
            index,
        )
        return self._with_parent_children(
            parent_folder_id,
            child_ids,
            folders=(*self.folders, folder),
        )

    def with_folder_renamed(self, folder_id: UUID, name: str) -> ConnectionCatalog:
        """Return a snapshot with a new folder label and unchanged descendants."""
        folder = self.folder_by_id(folder_id)
        if folder is None:
            raise ValueError("Connection folder does not exist")
        renamed = replace(folder, name=name)
        folders = tuple(
            renamed if current.folder_id == folder_id else current for current in self.folders
        )
        return replace(self, folders=folders)

    def without_empty_folder(self, folder_id: UUID) -> ConnectionCatalog:
        """Remove a folder only when doing so cannot delete hidden descendants."""
        folder = self.folder_by_id(folder_id)
        if folder is None:
            raise ValueError("Connection folder does not exist")
        if folder.child_ids:
            raise ValueError("Connection folder is not empty")
        parent_folder_id = self.parent_id_of(folder_id)
        child_ids = tuple(
            child_id for child_id in self._child_ids_of(parent_folder_id) if child_id != folder_id
        )
        folders = tuple(current for current in self.folders if current.folder_id != folder_id)
        return self._with_parent_children(
            parent_folder_id,
            child_ids,
            folders=folders,
        )

    def with_item_moved(
        self,
        item_id: UUID,
        *,
        parent_folder_id: UUID | None = None,
        index: int | None = None,
    ) -> ConnectionCatalog:
        """Move one item while preventing a folder from owning its descendants."""
        item = self._require_item(item_id)
        self._child_ids_of(parent_folder_id)
        if isinstance(item, ConnectionFolder) and (
            parent_folder_id == item_id
            or (
                parent_folder_id is not None
                and parent_folder_id in self._descendant_folder_ids(item.folder_id)
            )
        ):
            raise ValueError("Connection folder cannot be moved into itself or a descendant")

        current_parent_id = self.parent_id_of(item_id)
        current_children = tuple(
            child_id for child_id in self._child_ids_of(current_parent_id) if child_id != item_id
        )
        if current_parent_id == parent_folder_id:
            moved_children = self._insert_child(current_children, item_id, index)
            if moved_children == self._child_ids_of(current_parent_id):
                return self
            return self._with_parent_children(current_parent_id, moved_children)

        moved_children = self._insert_child(
            self._child_ids_of(parent_folder_id),
            item_id,
            index,
        )
        root_item_ids = self.root_item_ids
        if current_parent_id is None:
            root_item_ids = current_children
        elif parent_folder_id is None:
            root_item_ids = moved_children

        folders = tuple(
            replace(folder, child_ids=current_children)
            if folder.folder_id == current_parent_id
            else replace(folder, child_ids=moved_children)
            if folder.folder_id == parent_folder_id
            else folder
            for folder in self.folders
        )
        return ConnectionCatalog(
            root_item_ids=root_item_ids,
            folders=folders,
            profiles=self.profiles,
        )

    def _child_ids_of(self, parent_folder_id: UUID | None) -> tuple[UUID, ...]:
        """Resolve a root/folder child list and reject stale parent IDs."""
        if parent_folder_id is None:
            return self.root_item_ids
        folder = self.folder_by_id(parent_folder_id)
        if folder is None:
            raise ValueError("Parent connection folder does not exist")
        return folder.child_ids

    def _with_parent_children(
        self,
        parent_folder_id: UUID | None,
        child_ids: tuple[UUID, ...],
        *,
        folders: tuple[ConnectionFolder, ...] | None = None,
        profiles: tuple[ConnectionProfile, ...] | None = None,
    ) -> ConnectionCatalog:
        """Replace one child sequence while optionally replacing record tuples."""
        selected_folders = self.folders if folders is None else folders
        selected_profiles = self.profiles if profiles is None else profiles
        if parent_folder_id is None:
            return ConnectionCatalog(
                root_item_ids=child_ids,
                folders=selected_folders,
                profiles=selected_profiles,
            )
        selected_folders = tuple(
            replace(folder, child_ids=child_ids) if folder.folder_id == parent_folder_id else folder
            for folder in selected_folders
        )
        return ConnectionCatalog(
            root_item_ids=self.root_item_ids,
            folders=selected_folders,
            profiles=selected_profiles,
        )

    @staticmethod
    def _insert_child(
        child_ids: tuple[UUID, ...],
        item_id: UUID,
        index: int | None,
    ) -> tuple[UUID, ...]:
        """Insert at a checked sibling index, appending when it is omitted."""
        insertion_index = len(child_ids) if index is None else index
        if type(insertion_index) is not int or not 0 <= insertion_index <= len(child_ids):
            raise ValueError("Connection item index is out of range")
        return (*child_ids[:insertion_index], item_id, *child_ids[insertion_index:])

    def _item_exists(self, item_id: UUID) -> bool:
        """Return whether either item namespace owns a stable UUID."""
        return self.folder_by_id(item_id) is not None or self.profile_by_id(item_id) is not None

    def _require_item(self, item_id: UUID) -> ConnectionFolder | ConnectionProfile:
        """Return an existing item or reject an invalid/stale ID."""
        if not isinstance(item_id, UUID):
            raise ValueError("Connection item ID must be a UUID")
        item = self.folder_by_id(item_id) or self.profile_by_id(item_id)
        if item is None:
            raise ValueError("Connection catalog item does not exist")
        return item

    def _descendant_folder_ids(self, folder_id: UUID) -> set[UUID]:
        """Collect folder descendants for guarded move operations."""
        descendants: set[UUID] = set()
        folder = self.folder_by_id(folder_id)
        if folder is None:
            return descendants
        for child_id in folder.child_ids:
            child_folder = self.folder_by_id(child_id)
            if child_folder is not None:
                descendants.add(child_folder.folder_id)
                descendants.update(self._descendant_folder_ids(child_folder.folder_id))
        return descendants


def connection_folder_name_key(name: str) -> str:
    """Return the public canonical key used for sibling folder-name comparisons."""
    if not isinstance(name, str):
        raise TypeError("Connection folder name must be text")
    return unicodedata.normalize("NFC", name).casefold()


def connection_profile_copy_name(
    source_name: str,
    sibling_names: Iterable[str],
) -> str:
    """Return a bounded, collision-free display name for a copied profile.

    Sibling names share one visible namespace here, so callers should include both
    profile and folder labels. Comparisons match the catalog's Unicode-aware folder
    invariant even though profile names themselves are allowed to repeat elsewhere.
    """
    if not isinstance(source_name, str):
        raise TypeError("Connection profile source name must be text")
    _validate_profile_text(
        source_name,
        field_name="Connection profile source name",
        maximum_length=MAX_CONNECTION_NAME_CHARACTERS,
    )
    if isinstance(sibling_names, (str, bytes)) or not isinstance(sibling_names, Iterable):
        raise TypeError("Connection sibling names must be an iterable of text")

    reserved_name_keys: set[str] = set()
    for index, sibling_name in enumerate(sibling_names):
        if index >= MAX_CONNECTION_CATALOG_ITEMS:
            raise ValueError("Connection sibling names contain too many items")
        if not isinstance(sibling_name, str):
            raise TypeError("Connection sibling names must contain only text")
        reserved_name_keys.add(_connection_name_key(sibling_name))

    base_name = source_name
    copy_number = 1
    suffix_match = _CONNECTION_COPY_SUFFIX.fullmatch(source_name)
    if suffix_match is not None:
        series_base = suffix_match.group("base")
        matched_number = suffix_match.group("number")
        next_number = 2 if matched_number is None else int(matched_number) + 1
        last_bounded_attempt = next_number + len(reserved_name_keys)
        if _connection_copy_suffix_length(last_bounded_attempt) < (MAX_CONNECTION_NAME_CHARACTERS):
            base_name = series_base
            copy_number = next_number

    # At most one more candidate than the number of distinct reserved names is
    # needed: the generated candidates are distinct because their suffixes are.
    for _attempt in range(len(reserved_name_keys) + 1):
        candidate = _bounded_connection_copy_name(base_name, copy_number)
        if _connection_name_key(candidate) not in reserved_name_keys:
            return candidate
        copy_number += 1
    raise RuntimeError("Unable to allocate a connection profile copy name")


def _connection_name_key(name: str) -> str:
    """Normalize one connection-tree label for collision checks."""
    return unicodedata.normalize("NFC", name).casefold()


def _connection_copy_suffix_length(copy_number: int) -> int:
    """Return the display length reserved by one generated copy suffix."""
    return len(" copy") if copy_number == 1 else len(f" copy {copy_number}")


def _bounded_connection_copy_name(base_name: str, copy_number: int) -> str:
    """Compose one copy name while preserving its complete distinguishing suffix."""
    suffix = " copy" if copy_number == 1 else f" copy {copy_number}"
    prefix_length = MAX_CONNECTION_NAME_CHARACTERS - len(suffix)
    if prefix_length < 1:
        raise ValueError("Connection copy suffix is too long")
    bounded_base = base_name[:prefix_length].rstrip()
    if not bounded_base:
        # Valid source names cannot begin with whitespace, so this is reachable
        # only if a future caller changes the model's text invariant.
        raise ValueError("Connection profile source name cannot be copied safely")
    return f"{bounded_base}{suffix}"


def _validate_profile_text(
    value: object,
    *,
    field_name: str,
    maximum_length: int,
    forbid_whitespace: bool = False,
) -> None:
    """Validate persisted user text before it reaches SSH or Qt widgets."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    if len(value) > maximum_length:
        raise ValueError(f"{field_name} is too long")
    if value != value.strip():
        raise ValueError(f"{field_name} cannot start or end with whitespace")
    if forbid_whitespace and any(character.isspace() for character in value):
        raise ValueError(f"{field_name} cannot contain whitespace")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        or unicodedata.bidirectional(character) in _DIRECTIONAL_FORMATS
        for character in value
    ):
        raise ValueError(f"{field_name} contains unsafe control characters")
