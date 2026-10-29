"""Copy-only, resumable migration into a private home folder under both locks.

No SSH keys/trust databases or OS-vault secrets are imported implicitly. Older
versions keep their original data; a later legacy edit is a conflict, not a merge.
"""
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from termana.instance_lock import acquire_application_lock
from termana.storage.configuration_backup import (
    JOURNAL,
    PENDING,
    RECOVERY_JOURNAL,
    _directory,
    _read,
)
from termana.terminal.private_recovery_files import PrivateDirectory, PrivateFile

FILES = ('settings.json', 'connections.json', 'saved-commands.json', 'ssh-credential-index.json')
MARKER = 'home-data-migration.json'
PENDING_MIGRATION = 'home-data-migration.pending.json'
MAX_FILE_BYTES = 16 * 1024 * 1024


class DataDirectoryError(RuntimeError):
    pass


def _hash(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _read_private(directory, name):
    try:
        with PrivateFile(directory, name) as source:
            return source.read(MAX_FILE_BYTES)
    except FileNotFoundError:
        return None


def _publish_new(directory, name, data):
    with PrivateFile(directory, name, create=True) as target:
        target.rewrite(data)


def _migrate(directory, legacy):
    if not legacy.exists():
        return  # The legacy lock normally creates this directory first.
    _directory(legacy)
    if any((legacy / name).exists() for name in (JOURNAL, PENDING, RECOVERY_JOURNAL)):
        raise DataDirectoryError('Finish configuration recovery in the earlier Termana version before migrating. Originals are unchanged.')
    originals = {name: _read(legacy / name, MAX_FILE_BYTES, missing=True) for name in FILES}
    record = {'version': 1, 'source': str(legacy), 'files': {name: _hash(data) for name, data in originals.items()}}
    serialized = json.dumps(record, sort_keys=True).encode('utf-8')
    complete = _read_private(directory, MARKER)
    if complete is not None:
        if complete != serialized:
            raise DataDirectoryError('The old Termana data changed after migration. Both copies are retained; review the conflict before starting.')
        return
    pending = _read_private(directory, PENDING_MIGRATION)
    if pending is not None and pending != serialized:
        raise DataDirectoryError('Data changed during migration. Both copies are retained; review the conflict before starting.')
    # Check every destination before creating the journal or copying anything.
    for name, data in originals.items():
        current = _read_private(directory, name)
        if current is not None and current != data:
            raise DataDirectoryError('Both old and home folders contain different Termana data. Nothing was overwritten; choose the copy to keep.')
    if pending is None:
        _publish_new(directory, PENDING_MIGRATION, serialized)
    backup_path = directory.path / 'migration-originals'
    with PrivateDirectory(backup_path, parent=directory, create=not backup_path.exists(), writable=True) as backup:
        for name, data in {**originals, MARKER: serialized}.items():
            current = _read_private(backup, name)
            if current is not None and current != data:
                raise DataDirectoryError('Migration backup changed. Nothing will be overwritten.')
            if data is not None and current is None:
                _publish_new(backup, name, data)
    # Retain both the private backup and unchanged legacy files. Never remove them.
    # Exclusive writes + a pending manifest make interruption resumable, not a
    # license to replace a destination edited after an interrupted migration.
    for name, data in originals.items():
        if data is not None and _read_private(directory, name) is None:
            _publish_new(directory, name, data)
    if any(_read_private(directory, name) != data for name, data in originals.items()):
        raise DataDirectoryError('Home data could not be verified. Original files and the migration journal are retained.')
    if any(_read(legacy / name, MAX_FILE_BYTES, missing=True) != data for name, data in originals.items()):
        raise DataDirectoryError('Legacy data changed during migration. Original files and the journal are retained.')
    _publish_new(directory, MARKER, serialized)
    with PrivateFile(directory, PENDING_MIGRATION) as pending_file:
        pending_file.delete()


class DataOwnership:
    def __init__(self, stack):
        self._stack = stack

    def unlock(self):
        self._stack.close()


def acquire_data_ownership(path: Path, *, legacy: Path | None = None):
    """Hold current/legacy instance locks until the caller releases ownership."""
    if not path.is_absolute() or (legacy is not None and not legacy.is_absolute()):
        raise ValueError('Data paths must be absolute')
    stack = ExitStack()
    try:
        directory = stack.enter_context(PrivateDirectory(path, create=not path.exists(), writable=True))
        paths = {path / 'termana.lock'}
        if legacy is not None and legacy != path:
            paths.add(legacy / 'termana.lock')
        for lock_path in sorted(paths, key=str):
            lock = acquire_application_lock(lock_path)
            stack.callback(lock.unlock)
        if legacy is not None and legacy != path:
            _migrate(directory, legacy)
        return DataOwnership(stack)
    except Exception as error:
        stack.close()
        from termana.instance_lock import ApplicationLockError
        if isinstance(error, (ApplicationLockError, DataDirectoryError)):
            raise
        raise DataDirectoryError('Termana could not verify its private data folder or migration. Original data is retained; check folder permissions and migration state.') from error
