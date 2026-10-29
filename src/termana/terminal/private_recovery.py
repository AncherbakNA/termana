"""Explicit bounded private recovery, never a prefix/age/PID scavenger.

Only an exact registered object with an inactive exclusive lease is recoverable.
No SSH capability, remote metadata, credential or automatic startup action exists
here. Unknown/pre-journal leftovers remain untouched. This is not a CAS protocol.
"""

import hashlib
import json
import logging
import os
import re
import struct
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from termana.models.sftp import MAX_SFTP_EDIT_BYTES
from termana.terminal.private_recovery_files import (
    PrivateDirectory,
    PrivateFile,
    RecoveryError,
    security_digest,
)

MAX_RECOVERY_ENTRIES = 128
_EXPORT_TRANSACTION = "recovery-export"
_MAX_RECORD = 16384
_HEADER = struct.Struct(">8sQ32sI")
_MAGIC = b"TRDRAFT1"
_ENTRY = re.compile(r"(?:termana-edit-)?[0-9a-f]{32}\Z")
_LOG = logging.getLogger(__name__)
_WARNING = "Private recovery cleanup could not be confirmed"
_EXPORT_AUTHORITY = ContextVar("termana_private_recovery_export", default=None)


class RecoveryExportUnavailable(RecoveryError):
    pass


def default_recovery_root():
    from termana.paths import application_config_directory

    return application_config_directory() / "recovery"


def _json(data):
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _parse(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise RecoveryError()
            value[key] = item
        return value

    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise RecoveryError() from None


def _registry_matches(record, directory, marker):
    return (
        type(record) is dict
        and type(record.get("version")) is int
        and record == {"version": 1, "directory": directory.identity, "file": marker.identity}
    )


def _validate_parent(path):
    if os.name == "nt":
        from termana.credentials.windows_private_files import private_windows_directory

        with private_windows_directory(path):
            pass
    elif os.name == "posix":
        import stat

        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            status = os.fstat(descriptor)
            if status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) & 0o022:
                raise RecoveryError()
        finally:
            os.close(descriptor)
    else:
        raise RecoveryError()


@dataclass(frozen=True, slots=True)
class RecoveryOffer:
    identity: str
    kind: str
    size: int


class RecoveryStore:
    def __init__(self, root=None):
        self.path = Path(root) if root is not None else default_recovery_root()
        if not self.path.is_absolute() or self.path.parent == self.path:
            raise RecoveryError()
        self.directory = None
        _validate_parent(self.path.parent)
        try:
            try:
                self.directory = PrivateDirectory(self.path)
            except FileNotFoundError:
                with PrivateDirectory(self.path, create=True) as created:
                    with PrivateFile(created, "registry.json", create=True) as marker:
                        marker.rewrite(
                            _json(
                                {
                                    "version": 1,
                                    "directory": created.identity,
                                    "file": marker.identity,
                                }
                            )
                        )
                    created.sync()
                self.directory = PrivateDirectory(self.path)
            with PrivateFile(self.directory, "registry.json") as marker:
                record = _parse(marker.read(_MAX_RECORD))
                if not _registry_matches(record, self.directory, marker):
                    raise RecoveryError()
        except BaseException:
            self.close()
            raise

    def reserve(self):
        names = set(self.entry_names()) - {"registry.json", _EXPORT_TRANSACTION}
        if len(names) >= MAX_RECOVERY_ENTRIES:
            raise RecoveryError()

    def entry_names(self):
        # Exactly 128 ordinary entries + ONE fixed recovery-export transaction;
        # marker is not an entry. Unknown names still consume ordinary capacity.
        return self.directory.names(MAX_RECOVERY_ENTRIES + 2)

    @contextmanager
    def allocation(self, *, export_authority=None):
        # Serialize the hard entry budget without trusting PID or stale age.
        # The permanent marker is also the allocation lease; never unlink it.
        with PrivateFile(self.directory, "registry.json", lease=True) as marker:
            if not _registry_matches(_parse(marker.read(_MAX_RECORD)), self.directory, marker):
                raise RecoveryError()
            if export_authority is None:
                self.reserve()
            else:
                if type(export_authority) is not _ExportAuthority:
                    raise RecoveryExportUnavailable()
                export_authority.claim(self)
                if _EXPORT_TRANSACTION in self.entry_names():
                    raise RecoveryExportUnavailable()
            yield

    def create_editor(self, workspace):
        if Path(workspace.name).parent != self.path or not _ENTRY.fullmatch(
            Path(workspace.name).name
        ):
            raise RecoveryError()
        return OwnedRecovery(self, workspace=workspace)

    def create_stage(self, path, parent_handle, file_handle, *, export_authority=None):
        from termana.credentials.windows_private_files import _identity

        external = {
            "parent": str(path.parent),
            "name": path.name,
            "parent_id": _identity(parent_handle).hex(),
            "file_id": _identity(file_handle).hex(),
            "security": security_digest(file_handle),
        }
        with self.allocation(export_authority=export_authority):
            return OwnedRecovery(
                self,
                external=external,
                _entry_name=_EXPORT_TRANSACTION if export_authority is not None else None,
            )

    def close(self):
        if self.directory is not None:
            self.directory.close()
            self.directory = None


class OwnedRecovery:
    """Live exact owner. Closing a lease preserves; finish requires explicit discard."""

    def __init__(self, store, *, workspace=None, external=None, _entry_name=None):
        self.store, self.directory, self.lease = store, None, None
        self.record = None
        try:
            if workspace is not None:
                self.directory = PrivateDirectory(
                    workspace.name, borrowed_handle=workspace.recovery_directory_handle
                )
            else:
                self.directory = PrivateDirectory(
                    store.path / (_entry_name or uuid4().hex), parent=store.directory, create=True
                )
            self.lease = PrivateFile(self.directory, "lease", create=True, lease=True)
            files = {"lease": self.lease.identity}
            names = ("draft-a", "draft-b", "copy.record") if workspace is not None else ()
            for name in (*names, "record.json"):
                with PrivateFile(self.directory, name, create=True) as entry:
                    files[name] = entry.identity
            self.record = {
                "version": 1,
                "kind": "editor" if workspace is not None else "stage",
                "directory": self.directory.identity,
                "files": files,
                "external": external,
            }
            encoded = _json(self.record)
            if len(encoded) > _MAX_RECORD:
                raise RecoveryError()
            with PrivateFile(self.directory, "record.json") as entry:
                entry.rewrite(encoded)
            self.directory.sync()
            self.store.directory.sync()
        except BaseException:
            self.close()
            raise

    @classmethod
    def acquire(cls, store, name):
        value = cls.__new__(cls)
        value.store, value.directory, value.lease, value.record = store, None, None, None
        try:
            value.directory = PrivateDirectory(
                store.path / name, parent=store.directory, writable=True
            )
            value.lease = PrivateFile(value.directory, "lease", lease=True)
            with PrivateFile(value.directory, "record.json") as record:
                value.record = _parse(record.read(_MAX_RECORD))
            value.validate()
            return value
        except BaseException:
            value.close()
            raise

    def validate(self):
        self.directory.validate()
        self.lease.validate()
        record = self.record
        if (
            type(record) is not dict
            or set(record) != {"version", "kind", "directory", "files", "external"}
            or type(record["version"]) is not int
            or record["version"] != 1
            or record["kind"] not in ("editor", "stage")
            or record["directory"] != self.directory.identity
            or type(record["files"]) is not dict
        ):
            raise RecoveryError()
        expected = {"lease", "record.json"}
        if record["kind"] == "editor":
            expected |= {"draft-a", "draft-b", "copy.record"}
            if record["external"] is not None:
                raise RecoveryError()
        elif (
            type(record["external"]) is not dict
            or set(record["external"]) != {"parent", "name", "parent_id", "file_id", "security"}
            or any(type(item) is not str for item in record["external"].values())
        ):
            raise RecoveryError()
        if set(record["files"]) != expected or record["files"]["lease"] != self.lease.identity:
            raise RecoveryError()
        for name in expected - {"lease"}:
            with PrivateFile(self.directory, name) as entry:
                if record["files"][name] != entry.identity:
                    raise RecoveryError()
                if name == "record.json" and _parse(entry.read(_MAX_RECORD)) != record:
                    raise RecoveryError()

    def _file(self, name):
        entry = PrivateFile(self.directory, name)
        if self.record["files"].get(name) != entry.identity:
            entry.close()
            raise RecoveryError()
        return entry

    def register_copy(self):
        self.validate()
        with PrivateFile(self.directory, "copy.txt") as copy:
            identity = copy.identity
        with self._file("copy.record") as record:
            previous = record.read(_MAX_RECORD)
            if previous and _parse(previous) != {"copy": identity}:
                raise RecoveryError()
            record.rewrite(_json({"copy": identity}))
        self.directory.sync()

    def snapshot(self):
        self.validate()
        snapshots = []
        for name in ("draft-a", "draft-b"):
            with self._file(name) as entry:
                raw = entry.read(MAX_SFTP_EDIT_BYTES + _HEADER.size)
            if len(raw) < _HEADER.size:
                continue
            magic, sequence, digest, size = _HEADER.unpack_from(raw)
            data = raw[_HEADER.size :]
            if (
                magic == _MAGIC
                and 0 < sequence < 2**63
                and size == len(data)
                and hashlib.sha256(data).digest() == digest
            ):
                _text(data)
                snapshots.append((sequence, name, data))
        if not snapshots:
            return None
        snapshots.sort(key=lambda item: item[0])
        if len(snapshots) == 2 and snapshots[0][0] == snapshots[1][0]:
            raise RecoveryError()
        return snapshots[-1]

    def checkpoint(self, data):
        _text(data)
        previous = self.snapshot()
        if previous is not None and previous[2] == data:
            return
        sequence = previous[0] + 1 if previous else 1
        if sequence >= 2**63:
            raise RecoveryError()
        name = "draft-b" if previous and previous[1] == "draft-a" else "draft-a"
        payload = _HEADER.pack(_MAGIC, sequence, hashlib.sha256(data).digest(), len(data)) + data
        with self._file(name) as entry:
            entry.rewrite(payload)

    def disposal_plan(self):
        """Validate the entire bounded namespace before any destructive step."""
        self.validate()
        expected = set(self.record["files"])
        present = set(self.directory.names(16))
        copy_identity = None
        if self.record["kind"] == "editor" and "copy.txt" in present:
            with self._file("copy.record") as entry:
                copied = _parse(entry.read(_MAX_RECORD))
            if type(copied) is not dict or set(copied) != {"copy"}:
                raise RecoveryError()
            with PrivateFile(self.directory, "copy.txt") as entry:
                if entry.identity != copied["copy"]:
                    raise RecoveryError()
                copy_identity = entry.identity
            expected.add("copy.txt")
        if present != expected:
            raise RecoveryError()
        return expected, copy_identity

    def finish(self, *, remove_directory=False):
        """Discard only all exact registered children; unknown children preserve all."""
        expected, copy_identity = self.disposal_plan()
        # Complete validation precedes every destructive step. Each opened file
        # is checked again immediately before exact-handle deletion.
        for name in sorted(expected - {"lease", "record.json"}):
            with PrivateFile(self.directory, name) as entry:
                identity = copy_identity if name == "copy.txt" else self.record["files"][name]
                if entry.identity != identity:
                    raise RecoveryError()
                entry.delete()
        with self._file("record.json") as entry:
            entry.delete()
        self.lease.delete()
        self.lease.close()
        self.lease = None
        self.directory.sync()
        if remove_directory:
            self.directory.remove(self.store.directory)
        self.close()

    def close(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None
        if self.directory is not None:
            self.directory.close()
            self.directory = None


def _text(data):
    if type(data) is not bytes or len(data) > MAX_SFTP_EDIT_BYTES or b"\0" in data:
        raise RecoveryError()
    try:
        data.decode("utf-8")
    except UnicodeError:
        raise RecoveryError() from None


@dataclass(slots=True, repr=False)
class _ExportAuthority:
    session: object
    identity: str
    owned: object
    marker: tuple
    target: Path
    root_identity: str
    used: bool = False
    unavailable: bool = False

    def claim(self, store):
        session = self.session
        if (
            self is not _EXPORT_AUTHORITY.get()
            or type(session) is not RecoverySession
            or type(self.owned) is not OwnedRecovery
            or self.used
            or session._store is None
            or session._items.get(self.identity) != (self.owned, self.marker)
            or session._store.directory.identity != self.root_identity
            or store.directory.identity != self.root_identity
        ):
            raise RecoveryExportUnavailable()
        snapshot = self.owned.snapshot()  # Exact owner, lease, IDs, ACLs and revision.
        if snapshot is None or (snapshot[0], hashlib.sha256(snapshot[2]).digest()) != self.marker:
            raise RecoveryExportUnavailable()
        self.used = True


class RecoverySession:
    """An explicit, lease-owning review. Public values contain no paths or text."""

    def __init__(self, root=None):
        self._store, self._items, self.offers = None, {}, ()
        try:
            self._store = RecoveryStore(root)
            offers = []
            for name in self._store.entry_names():
                if not _ENTRY.fullmatch(name) and name != _EXPORT_TRANSACTION:
                    continue
                owned = None
                try:
                    owned = OwnedRecovery.acquire(self._store, name)
                    if name == _EXPORT_TRANSACTION and owned.record["kind"] != "stage":
                        raise RecoveryError()
                    if owned.record["kind"] == "editor":
                        snapshot = owned.snapshot()
                        if snapshot is None:
                            owned.close()
                            continue
                        marker = (snapshot[0], hashlib.sha256(snapshot[2]).digest())
                        offer = RecoveryOffer(name, "editor", len(snapshot[2]))
                    else:
                        present = self._stage(owned, delete=False)
                        marker = present
                        offer = RecoveryOffer(name, "stage" if present else "receipt", 0)
                    self._items[name] = (owned, marker)
                    offers.append(offer)
                except Exception:
                    if owned is not None:
                        owned.close()
                    continue  # Live, malformed or unknown objects are preserved.
            self.offers = tuple(offers)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _stage(owned, *, delete):
        from termana.credentials.windows_private_files import (
            _delete,
            _identity,
            _open,
            _write_api,
            private_windows_directory,
        )

        external = owned.record["external"]
        with private_windows_directory(Path(external["parent"])) as parent:
            if _identity(parent).hex() != external["parent_id"]:
                raise RecoveryError()
            try:
                handle = _open(external["name"], parent=parent, writable=True)
            except FileNotFoundError:
                # ONLY exact old-leaf absence under this validated original
                # parent. The published destination is not known or inspected.
                return False
            try:
                if (
                    _identity(handle).hex() != external["file_id"]
                    or security_digest(handle) != external["security"]
                ):
                    raise RecoveryError()
                # Link count validation through the exact handle, not a path.
                import msvcrt

                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
                handle = None
                try:
                    if os.fstat(fd).st_nlink != 1:
                        raise RecoveryError()
                    if delete:
                        _delete(msvcrt.get_osfhandle(fd))
                finally:
                    os.close(fd)
            finally:
                if handle is not None:
                    _write_api().CloseHandle(handle)
        return True

    def export(self, identity, target):
        from termana.terminal.private_text_export import export_private_text_copy

        owned, marker = self._items[identity]
        if owned.record["kind"] != "editor":
            raise RecoveryError()
        snapshot = owned.snapshot()
        if snapshot is None or (snapshot[0], hashlib.sha256(snapshot[2]).digest()) != marker:
            raise RecoveryError()
        authority = _ExportAuthority(
            self, identity, owned, marker, target, self._store.directory.identity
        )
        token = _EXPORT_AUTHORITY.set(authority)
        try:
            export_private_text_copy(target, snapshot[2])
        except Exception:
            if authority.unavailable:
                raise RecoveryExportUnavailable() from None
            raise
        finally:
            _EXPORT_AUTHORITY.reset(token)

    def discard(self, identity):
        owned, marker = self._items[identity]
        if owned.record["kind"] == "editor":
            snapshot = owned.snapshot()
            if snapshot is None or (snapshot[0], hashlib.sha256(snapshot[2]).digest()) != marker:
                raise RecoveryError()
        owned.disposal_plan()
        if owned.record["kind"] == "stage":
            present = self._stage(owned, delete=False)
            if type(marker) is not bool or present is not marker:
                raise RecoveryError()
            if marker:
                self._stage(owned, delete=True)
        owned.finish(remove_directory=True)
        del self._items[identity]
        self.offers = tuple(offer for offer in self.offers if offer.identity != identity)

    def close(self):
        for owned, _marker in self._items.values():
            owned.close()
        self._items.clear()
        self.offers = ()
        if self._store is not None:
            self._store.close()
            self._store = None


def register_windows_stage(path, parent_handle, file_handle, *, publication_target=None):
    candidate = _EXPORT_AUTHORITY.get()
    authority = (
        candidate
        if (
            type(candidate) is _ExportAuthority
            and publication_target == candidate.target
            and path.parent == candidate.target.parent
            and re.fullmatch(r"\.termana-export-[0-9a-f]{32}", path.name)
        )
        else None
    )
    store = None
    try:
        store = RecoveryStore(authority.owned.store.path if authority is not None else None)
        return store.create_stage(path, parent_handle, file_handle, export_authority=authority)
    except BaseException:
        if authority is not None:
            authority.unavailable = True
        if store is not None:
            store.close()
        raise


def finish_stage(owned):
    if owned is None:
        return
    try:
        owned.finish(remove_directory=True)
    except Exception:
        _LOG.warning(_WARNING)
        owned.close()
    finally:
        owned.store.close()
