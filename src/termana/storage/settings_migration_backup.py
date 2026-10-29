"""Preserve validated original settings bytes before an explicit schema upgrade."""

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from termana.storage.json_objects import reject_duplicate_json_keys


class SettingsMigrationBackupError(RuntimeError):
    pass


def _read(path, limit):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
    ):
        raise SettingsMigrationBackupError(
            "Linked or non-regular settings cannot be replaced safely."
        )
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            raise SettingsMigrationBackupError("Settings changed while being read.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SettingsMigrationBackupError("Existing settings exceed the supported size limit.")
    return data


def _directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SettingsMigrationBackupError("Settings backup directory is not a regular directory.")


@dataclass(frozen=True)
class PreparedSettingsWrite:
    path: Path
    original: bytes | None = field(repr=False)
    limit: int
    backup: Path | None = None

    def assert_unchanged(self):
        if _read(self.path, self.limit) != self.original:
            raise SettingsMigrationBackupError(
                "Settings changed before saving; no replacement was made."
            )


def preserve_before_schema_change(path, *, current_version, limit, validate, label="settings"):
    """No load-time writes, no retention deletion, one content-addressed old copy.

    Call under the application lock. The second read detects incidental external
    edits; it is not an atomic compare-and-swap against arbitrary other writers.
    """
    if label not in {"settings", "connections", "ssh-credential-index"}:
        raise ValueError("Unsupported migration snapshot label")
    raw = _read(path, limit)
    if raw is None:
        return PreparedSettingsWrite(path, None, limit)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
        validate(document)
        version = document["schema_version"]
    except Exception:
        raise SettingsMigrationBackupError(
            "Existing settings are invalid or from an unsupported version; they were not overwritten."
        ) from None
    if version == current_version:
        return PreparedSettingsWrite(path, raw, limit)
    if type(version) is not int or not 1 <= version < current_version:
        raise SettingsMigrationBackupError("Unsupported settings version; no replacement was made.")
    parent = path.parent
    parent_info = parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or getattr(parent_info, "st_file_attributes", 0) & 0x400
    ):
        raise SettingsMigrationBackupError("Settings directory is not a regular directory.")
    backups = parent / "configuration-backups"
    _directory(backups)
    migrations = backups / f"{label}-migrations"
    _directory(migrations)
    digest = hashlib.sha256(raw).hexdigest()
    destination = migrations / f"{label}-v{version}-{digest}.json"
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _read(destination, limit) != raw:
            raise SettingsMigrationBackupError(
                "The existing settings backup is inconsistent."
            ) from None
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    # An interrupted/partial backup must never authorize a schema replacement.
    if _read(destination, limit) != raw:
        raise SettingsMigrationBackupError("The settings backup could not be verified.")
    return PreparedSettingsWrite(path, raw, limit, destination)
