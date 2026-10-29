"""Non-secret, bounded messages for one live SSH owner's file channel."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

MAX_SFTP_PENDING = 16
MAX_SFTP_ENTRIES = 1000
MAX_SFTP_PATH_CHARACTERS = 4096
MAX_SFTP_LISTING_NAME_BYTES = 256 * 1024
SFTP_CHUNK_BYTES = 64 * 1024
MAX_SFTP_EDIT_BYTES = 2 * 1024 * 1024


class SftpOperation(StrEnum):
    LIST = "list"
    MKDIR = "mkdir"
    DOWNLOAD = "download"
    UPLOAD = "upload"
    EDIT_DOWNLOAD = "edit-download"
    EDIT_UPLOAD = "edit-upload"
    RENAME = "rename"
    DOWNLOAD_TREE = "download-tree"
    UPLOAD_TREE = "upload-tree"

    @property
    def is_tree(self):
        return self in (self.DOWNLOAD_TREE, self.UPLOAD_TREE)

    @property
    def label(self):
        return _OPERATION_LABELS[self]


_OPERATION_LABELS = MappingProxyType(
    {
        SftpOperation.LIST: "Folder listing",
        SftpOperation.MKDIR: "Create folder",
        SftpOperation.DOWNLOAD: "Download",
        SftpOperation.UPLOAD: "Upload",
        SftpOperation.EDIT_DOWNLOAD: "Open text for editing",
        SftpOperation.EDIT_UPLOAD: "Save edited text",
        SftpOperation.RENAME: "Rename/move file",
        SftpOperation.DOWNLOAD_TREE: "Download folder",
        SftpOperation.UPLOAD_TREE: "Upload folder",
    }
)


class SftpTreePolicy(StrEnum):
    ASK = "ask"
    SKIP = "skip"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class SftpFileVersion:
    size: int
    digest: bytes = field(repr=False)

    def __post_init__(self):
        if (
            type(self.size) is not int
            or not 0 <= self.size <= MAX_SFTP_EDIT_BYTES
            or type(self.digest) is not bytes
            or len(self.digest) != 32
        ):
            raise ValueError("Invalid edited-file version")


@dataclass(frozen=True, slots=True)
class SftpRequest:
    operation: SftpOperation
    remote_path: str
    local_path: Path | None = None
    overwrite: bool = False
    request_id: UUID = field(default_factory=uuid4)
    target_path: str | None = None
    version: SftpFileVersion | None = None
    edit_download_target: object | None = field(default=None, repr=False, compare=False)
    tree_policy: SftpTreePolicy = SftpTreePolicy.ASK

    def __post_init__(self):
        if (
            type(self.operation) is not SftpOperation
            or type(self.remote_path) is not str
            or not self.remote_path
            or len(self.remote_path) > MAX_SFTP_PATH_CHARACTERS
            or "\0" in self.remote_path
            or type(self.overwrite) is not bool
            or type(self.request_id) is not UUID
        ):
            raise ValueError("Invalid file request")
        if self.edit_download_target is not None and (
            self.operation is not SftpOperation.EDIT_DOWNLOAD or self.overwrite
        ):
            raise ValueError("A private edit target requires a non-overwriting edited download")
        if type(self.tree_policy) is not SftpTreePolicy or (
            not self.operation.is_tree and self.tree_policy is not SftpTreePolicy.ASK
        ):
            raise ValueError("A folder conflict policy belongs only to a folder transfer")
        if self.operation.is_tree and (
            self.overwrite
            or not self.remote_path.startswith("/")
            or self.remote_path == "/"
            or self.remote_path.endswith("/")
            or "//" in self.remote_path
            or any(part in (".", "..") for part in self.remote_path.split("/"))
        ):
            raise ValueError("Folder transfers require an explicit path and folder conflict policy")
        try:
            self.remote_path.encode("utf-8")
        except UnicodeError:
            raise ValueError("Remote paths must be valid UTF-8") from None
        if self.operation is SftpOperation.RENAME:
            for path in (self.remote_path, self.target_path):
                if (
                    type(path) is not str
                    or not path.startswith("/")
                    or path.endswith("/")
                    or len(path) > MAX_SFTP_PATH_CHARACTERS
                    or "\0" in path
                    or any(part in (".", "..") for part in path.split("/"))
                ):
                    raise ValueError("Rename requires two explicit absolute file paths")
                path.encode("utf-8")
            if (
                self.local_path is not None
                or self.overwrite
                or self.version is not None
                or self.remote_path == self.target_path
            ):
                raise ValueError("Rename never overwrites or transfers local files")
            return
        if self.target_path is not None:
            raise ValueError("A target path is only valid for rename")
        if self.operation is SftpOperation.EDIT_UPLOAD:
            if type(self.version) is not SftpFileVersion or not self.overwrite:
                raise ValueError("Edited save requires its original version and explicit overwrite")
        elif self.version is not None:
            raise ValueError("Only edited save accepts a remote version")
        if self.operation in (SftpOperation.LIST, SftpOperation.MKDIR):
            if self.local_path is not None or self.overwrite:
                raise ValueError("Directory requests cannot transfer or overwrite files")
            if self.operation is SftpOperation.MKDIR and (
                not self.remote_path.startswith("/")
                or self.remote_path.endswith("/")
                or any(part in (".", "..") for part in self.remote_path.split("/"))
            ):
                raise ValueError("Folder creation requires an explicit absolute remote path")
        elif (
            not isinstance(self.local_path, Path)
            or not self.local_path.is_absolute()
            or "\0" in str(self.local_path)
        ):
            raise ValueError("Transfers require an explicit absolute local path")
        if self.operation.is_tree and (not self.local_path.name or ".." in self.local_path.parts):
            raise ValueError("Choose a named local folder, not a filesystem root")


@dataclass(frozen=True, slots=True)
class SftpEntry:
    name: str
    kind: str
    size: int | None
    permissions: int | None = None
    modified: int | None = None


@dataclass(frozen=True, slots=True)
class SftpTreeSummary:
    files_completed: int = 0
    files_total: int = 0
    directories_created: int = 0
    skipped_existing: int = 0
    skipped_links: int = 0
    transferred: int = 0
    total: int = 0


@dataclass(frozen=True, slots=True)
class SftpResult:
    request_id: UUID
    status: str
    message: str = ""
    directory: str = ""
    entries: tuple[SftpEntry, ...] = ()
    truncated: bool = False
    omitted_entries: int = 0
    version: SftpFileVersion | None = None
    tree_summary: SftpTreeSummary | None = None


@dataclass(frozen=True, slots=True)
class SftpProgress:
    request_id: UUID
    transferred: int
    total: int | None
    files_completed: int = 0
    files_total: int = 0
    phase: str = "transfer"
    directories_completed: int = 0
    directories_total: int = 0


@dataclass(frozen=True, slots=True)
class SftpTransferUpdate:
    """GUI snapshot from one browser; routing still belongs to its exact backend."""

    request: SftpRequest
    status: str
    pending: bool
    transferred: int = 0
    total: int | None = None
    result_status: str | None = None
