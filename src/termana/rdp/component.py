"""Pinned source or bundled component; never search PATH or accept an override."""

import hashlib
import os
import platform
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

# Updated only after a reviewed isolated component build. Not a distribution manifest.
CLIENT_SHA256 = "d3b57a916dd6cddc205597ccdef93516d569d381c0a37f514293425ebc073dea"
WINDOWS_CLIENT_SHA256 = "843ab153532ba2cf8a84118d82ab61a518d66d1adec89e91cc4f42ac3ae10847"
_COMPONENTS = {
    ("darwin", "arm64"): ("helper-darwin-arm64-public-r1", CLIENT_SHA256),
    ("linux", "x86_64"): ("helper-linux-x64-clipfix-r1",
                          "f71b6ec3c2bf4877ef5f5a3ad0fa77a508297cebc69aade370774ec06c1f3848"),
    ("win32", "AMD64"): ("helper-windows-x64-network-r2", WINDOWS_CLIENT_SHA256),
    ("win32", "x86_64"): ("helper-windows-x64-network-r2", WINDOWS_CLIENT_SHA256),
}


def _component_spec(system, machine):
    try:
        label, checksum = _COMPONENTS[system, machine]
    except KeyError:
        raise RuntimeError("No accepted RDP component is available for this platform/architecture") from None
    name = "termana-rdp-client.exe" if system == "win32" else "termana-rdp-client"
    return Path("build/rdp-core-20261026") / label / "compile" / name, checksum


def bundle_directory(system, machine):
    """Canonical resource name, shared by the build and frozen loader."""
    _component_spec(system, machine)  # Refuse unsupported architectures first.
    architecture = "x86_64" if machine == "AMD64" else machine
    return Path("termana/rdp_components") / f"{system}-{architecture}"


def _selected_component_path(system, machine):
    relative, checksum = _component_spec(system, machine)
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[3] / relative, checksum
    if system == "darwin":
        # Inserted after PyInstaller processing, then outer-only sealed. Do not
        # follow its Frameworks/Resources cross-links or accept re-signed bytes.
        executable = Path(sys.executable)
        if executable.parent.name != "MacOS" or executable.parent.parent.name != "Contents":
            raise RuntimeError("RDP component requires a complete application bundle")
        return executable.parent.parent / "Helpers" / relative.name, checksum
    elif system == "win32":
        base = Path(getattr(sys, "_MEIPASS", ""))
        if not base.is_absolute():
            raise RuntimeError("RDP component requires the application resource directory")
    else:
        raise RuntimeError("No accepted frozen RDP component is available for this platform")
    return base / bundle_directory(system, machine) / relative.name, checksum


@contextmanager
def windows_component_lease(path, checksum):
    """Pin the actual no-reparse file and chosen ancestors through process exit.

    The drive's volume root is not renameable. Pin every chosen child directory
    without delete sharing instead of rejecting normal rights to create unrelated
    siblings at C:\\. Each chosen child must deny untrusted writes; no ACL repair.
    """
    from termana.credentials.windows_private_files import private_windows_directory
    from termana.credentials.windows_ssh_files import (
        open_windows_ssh_file,
        validate_windows_ssh_file_path,
        windows_ssh_security_stamp,
    )

    validate_windows_ssh_file_path(path)  # Reject devices/UNC/streams before any open.
    with ExitStack() as leases:
        for directory in reversed(path.parents):
            if directory != Path(path.anchor):
                leases.enter_context(private_windows_directory(directory, sharing=3))
        descriptor = open_windows_ssh_file(path)  # Kernel no-reparse; no write/delete sharing.
        try:
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        leases.enter_context(stream)
        stamp = windows_ssh_security_stamp(stream.fileno(), private=False)
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 100_000_000
                or info.st_nlink != 1):
            raise RuntimeError("RDP component is not a private verified executable")
        if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
            raise RuntimeError("RDP component build does not match the reviewed version")
        yield path
        if windows_ssh_security_stamp(stream.fileno(), private=False) != stamp:
            raise RuntimeError("RDP component security changed while running")


def _verify_posix_component(path, checksum):
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise RuntimeError("RDP component path must not contain symlinks")
        metadata = candidate.stat()
        if metadata.st_mode & 0o022 or metadata.st_uid not in (0, os.getuid()):
            raise RuntimeError("RDP component path is writable by another identity")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or not os.access(path, os.X_OK)
            or info.st_size > 100_000_000):
        raise RuntimeError("RDP component is not a private verified executable")
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != checksum:
        raise RuntimeError("RDP component build does not match the reviewed version")


def verified_component() -> Path:
    path, checksum = _selected_component_path(sys.platform, platform.machine())
    if sys.platform == "win32":
        with windows_component_lease(path, checksum):
            pass
    else:
        _verify_posix_component(path, checksum)
    return path
