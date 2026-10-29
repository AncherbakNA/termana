"""Reviewed package-owned SSH1 component identities and historical pin probe.

The GUI uses the authenticated frontend via LegacySshBackend. The historical
LegacySshPlan remains for the independent transport experiment only. Neither
path searches PATH, inherits PuTTY settings or falls back to SSH2.
"""

import hashlib
import os
import platform
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from termana.models.ssh_credential import validate_ssh_username
from termana.models.ssh_jump import validate_ssh_route_host
from termana.terminal.command import ShellCommand

_MD5 = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){15}\Z")
_REVIEWED_BUILDS = {
    ("darwin", "arm64"): "9b932bb0ddf5e6114a87dc7f11fcd7f25968eb9ec6ea60d303162cf7b75fc4fc",
}
_REVIEWED_FRONTENDS = {
    ("darwin", "arm64"): "47c1114a7547d0d5c55b4dfe173f78acaff231f82d73b2fb8ffb957100fef876",
    ("linux", "x86_64"): "9e88e873eadb42bb8f20b0929ad2ce9781fcdb77f66964790ffb5072765c1cbf",
    ("win32", "AMD64"): "10ade77aaebf73f659f84fb2485f70883df1f4472cd1ecad353b3a5d47e96cdb",
}


def legacy_helper_path() -> Path:
    """Package-owned optional component, never PATH or a user-supplied command."""
    name = "termana-ssh1-helper.exe" if sys.platform == "win32" else "termana-ssh1-helper"
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        executable = Path(sys.executable)
        if executable.parent.name != "MacOS" or executable.parent.parent.name != "Contents":
            raise LegacySshError("SSH1 requires a complete application bundle.")
        return executable.parent.parent / "Helpers" / name
    return Path(__file__).resolve().parents[1] / "legacy_helpers" / f"{sys.platform}-{platform.machine().lower()}" / name


def verify_legacy_frontend(path: Path) -> str:
    expected = _REVIEWED_FRONTENDS.get((sys.platform, platform.machine()))
    if expected is None:
        raise LegacySshError("A reviewed SSH1 frontend is not available for this platform yet.")
    if os.name == "nt":
        from termana.credentials.embedded_ssh_trust import read_embedded_trust_snapshot

        try:
            data, status = read_embedded_trust_snapshot(path, max_bytes=16 * 1024 * 1024)
            if status.st_nlink != 1 or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            raise LegacySshError("The SSH1 helper could not be verified safely.") from None
        return str(path)
    return _verify_posix_helper(path, expected)


class LegacySshError(ValueError):
    """Fixed public guidance, never raw helper output or credential data."""


def _identity(status):
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mode,
        status.st_uid,
        status.st_nlink,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def verify_legacy_helper(path: Path) -> str:
    """Allow only the exact reviewed build for this native platform.

    Installation/lifecycle must additionally prevent replacing the helper while
    sessions start. The stat/digest checks do not promise protection from a fully
    compromised account which can replace its application after validation.
    """
    expected = _REVIEWED_BUILDS.get((sys.platform, platform.machine()))
    if expected is None:
        raise LegacySshError("A reviewed SSH1 helper is not available for this platform yet.")
    return _verify_posix_helper(path, expected)


def _verify_posix_helper(path: Path, expected: str) -> str:
    if not isinstance(path, Path) or not path.is_absolute():
        raise LegacySshError("The SSH1 helper must have an approved absolute location.")
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > 16 * 1024 * 1024
            or before.st_mode & 0o022
            or before.st_uid not in (0, os.getuid())
            or not os.access(path, os.X_OK)
        ):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if _identity(before) != _identity(opened):
                raise ValueError
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise ValueError
                remaining -= len(chunk)
                digest.update(chunk)
            after = path.lstat()
            if (
                _identity(before) != _identity(after)
                or _identity(os.fstat(fd)) != _identity(opened)
                or digest.hexdigest() != expected
            ):
                raise ValueError
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise LegacySshError(
            "The SSH1 helper is missing, changed or could not be verified safely."
        ) from None
    return str(path)


@dataclass(frozen=True, slots=True)
class LegacySshPlan:
    host: str
    port: int
    username: str
    fingerprint: str
    insecure_protocol_approved: bool = False

    def __post_init__(self):
        validate_ssh_route_host(self.host)
        validate_ssh_username(self.username)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise LegacySshError("Choose a valid SSH1 port.")
        if type(self.fingerprint) is not str or _MD5.fullmatch(self.fingerprint) is None:
            raise LegacySshError(
                "SSH1 requires one exact verified legacy MD5 host-key fingerprint."
            )
        if self.insecure_protocol_approved is not True:
            raise LegacySshError("SSH1 is insecure and needs explicit per-launch consent.")

    def command(self, helper: Path) -> ShellCommand:
        return ShellCommand(
            verify_legacy_helper(helper),
            (
                "-ssh",
                "-1",
                "-t",
                "-noagent",
                "-a",
                "-x",
                "-noshare",
                "-no-antispoof",
                "-hostkey",
                self.fingerprint.lower(),
                "-P",
                str(self.port),
                "-l",
                self.username,
                self.host,
            ),
        )
