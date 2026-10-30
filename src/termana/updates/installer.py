"""User-owned desktop replacement with verified bytes and recoverable rename.

No elevation, shell scripts, PATH lookup, source-tree replacement or automatic
session termination. A separate copy of the running app waits for stdin EOF;
the GUI keeps that pipe open until its normal process exit.
"""

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.request import Request, build_opener
from uuid import uuid4

from termana.storage.atomic_file import replace_file_atomically
from termana.updates.client import _SafeRedirects, _validate_url, verify_package
from termana.updates.manifest import UpdateError, read_signed_manifest
from termana.updates.official import PUBLIC_KEY, REPOSITORY, native_target

MAX_EXPANDED = 2 * 1024**3
MAX_MEMBERS = 20000
_HANDOFFS = []  # Hold EOF pipes until interpreter shutdown, never widget GC.


def secure_directory(path):
    """Check, never repair, existing ancestors. Windows uses native no-reparse ACLs."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise UpdateError("Install in a local folder owned by your account")
    if os.name == "nt":
        from termana.credentials.windows_private_files import private_windows_directory

        for parent in (*reversed(path.parents), path):
            if parent != Path(path.anchor):
                with private_windows_directory(parent):
                    pass
    else:
        for parent in (*reversed(path.parents), path):
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022
            ):
                raise UpdateError(
                    "Install in your own Applications folder; shared or linked paths are not supported"
                )
        if path.stat().st_uid != os.getuid():
            raise UpdateError("Automatic updates require a folder owned by your account")


def installed_target():
    if not getattr(sys, "frozen", False):
        raise UpdateError(
            "Source launches are updated from source; automatic installation requires the packaged app"
        )
    executable = Path(sys.executable)
    if sys.platform == "darwin":
        target = executable.parent.parent.parent
        if (
            target.name != "Termana.app"
            or executable.relative_to(target).as_posix() != "Contents/MacOS/Termana"
        ):
            raise UpdateError("Keep the complete Termana.app bundle when installing")
    elif sys.platform == "win32":
        target = executable
        if target.suffix.lower() != ".exe":
            raise UpdateError("Unsupported Windows application location")
    else:
        raise UpdateError(
            "Automatic installation is currently available for macOS and Windows packages"
        )
    secure_directory(target.parent)
    return target


def download_package(manifest, artifact, destination, cancelled, *, opener=None):
    url = manifest.package_url(artifact)
    _validate_url(url)
    opener = opener or build_opener(_SafeRedirects())
    request = Request(
        url, headers={"Accept-Encoding": "identity", "User-Agent": "Termana-Update/1"}
    )
    deadline = time.monotonic() + 900
    try:
        with opener.open(request, timeout=15) as response, destination.open("xb") as output:
            os.chmod(destination, 0o600)
            _validate_url(response.geturl())
            if (
                response.status != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise UpdateError("Update server did not provide an uncompressed package")
            size, digest = 0, hashlib.sha256()
            while True:
                if cancelled.is_set():
                    raise UpdateError(
                        "Update download cancelled; the installed app was not changed"
                    )
                if time.monotonic() >= deadline:
                    raise UpdateError("Update download timed out")
                chunk = response.read(min(65536, artifact.size + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > artifact.size:
                    raise UpdateError("Downloaded package exceeds its signed size")
                output.write(chunk)
                digest.update(chunk)
            if size != artifact.size or digest.hexdigest() != artifact.sha256:
                raise UpdateError(
                    "Downloaded package failed signature-bound size or checksum verification"
                )
            output.flush()
            os.fsync(output.fileno())
    except UpdateError:
        raise
    except Exception:
        raise UpdateError(
            "Could not download the update securely; the installed app was not changed"
        ) from None


def extract_mac_package(package, destination):
    """Closed bundle-root ZIP, links resolved inside bundle, no link-first writes."""
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_MEMBERS or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise UpdateError("Update archive exceeds its extraction limits")
        names, links = set(), []
        for item in entries:
            name = PurePosixPath(item.filename)
            normalized = name.as_posix().rstrip("/")
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in item.filename
                or not name.parts
                or name.parts[0] != "Termana.app"
                or any(":" in part or "\x00" in part for part in name.parts)
                or normalized.casefold() in names
                or item.flag_bits & 1
            ):
                raise UpdateError("Unsafe update archive entry")
            names.add(normalized.casefold())
            mode = item.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                raise UpdateError("Unsupported update archive entry")
            path = destination.joinpath(*name.parts)
            if stat.S_ISLNK(mode):
                if item.file_size > 4096:
                    raise UpdateError("Oversized bundle symlink")
                target = archive.read(item).decode("utf-8")
                if (
                    len(target) > 4096
                    or "\\" in target
                    or "\x00" in target
                    or PurePosixPath(target).is_absolute()
                ):
                    raise UpdateError("Unsafe bundle symlink")
                resolved = (path.parent / target).resolve()
                if not resolved.is_relative_to(destination / "Termana.app"):
                    raise UpdateError("Bundle symlink leaves the application")
                links.append((path, target))
                continue
            if item.is_dir():
                path.mkdir(mode=0o755, parents=True, exist_ok=True)
            else:
                path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with archive.open(item) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output, 65536)
                path.chmod(0o755 if mode & 0o111 else 0o644)
        for path, target in links:
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.symlink_to(target)
        # Resolve complete chains AFTER all links exist; reject dangling/cyclic
        # or escaping chains, not just each link's first lexical destination.
        root = destination / "Termana.app"
        for path, _ in links:
            try:
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError()
            except (OSError, ValueError, RuntimeError):
                raise UpdateError("Invalid bundle symlink chain") from None
    return destination / "Termana.app"


def validate_mac_bundle(bundle):
    try:
        with (bundle / "Contents/Info.plist").open("rb") as source:
            info = plistlib.load(source)
        if (
            info.get("CFBundleIdentifier") != "org.termana.desktop"
            or info.get("CFBundleExecutable") != "Termana"
        ):
            raise ValueError()
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(bundle)],
            check=True,
            capture_output=True,
            timeout=45,
        )
    except Exception:
        raise UpdateError("The downloaded macOS application failed its integrity check") from None


def _new_file(path, data):
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    if os.name != "nt":
        path.chmod(0o600)


def check_package_runtime(executable, report, job, cancelled, *, expected_version=None):
    """Bounded, cancelable self-test process; never an interactive user session."""
    process = subprocess.Popen(
        [str(executable), "--check-update-runtime", str(report)],
        cwd=job,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + 180
    try:
        while process.poll() is None:
            if cancelled.wait(0.1):
                raise UpdateError("Update preparation cancelled; the installed app was not changed")
            if time.monotonic() >= deadline:
                raise UpdateError("The update startup check timed out")
        if process.returncode != 0:
            raise UpdateError(
                "The new app did not pass its isolated startup check; the installed app was not changed"
            )
        raw = report.read_bytes()
        if len(raw) > 4096:
            raise UpdateError("Invalid update startup report")
        result = json.loads(raw)
        if (
            result.get("passed") is not True
            or result.get("user_data_accessed") is not False
            or result.get("remote_connections") is not False
            or (expected_version is not None and result.get("version") != expected_version)
        ):
            raise UpdateError("The new app did not confirm isolated startup")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def stage_update(result, target, cancelled, *, downloader=download_package, metadata_fetch=None):
    """All fallible download/extraction work happens BEFORE closing sessions."""
    from termana.updates.client import fetch_public_metadata

    fetch = metadata_fetch or fetch_public_metadata
    if result.status != "available" or result.artifact is None:
        raise UpdateError("No newer compatible package is ready to install")
    secure_directory(target.parent)
    system, architecture = native_target()
    if (result.artifact.system, result.artifact.architecture) != (system, architecture):
        raise UpdateError("Update package does not match this computer")
    if (system, result.artifact.package) not in {("macos", "zip"), ("windows", "exe")}:
        raise UpdateError("This package format needs manual installation")
    tag = "v" + result.manifest.version
    raw = fetch(REPOSITORY.asset_url(tag, "manifest.json"), 32768)
    signature = fetch(REPOSITORY.asset_url(tag, "manifest.sig"), 64)
    verified = read_signed_manifest(
        raw, signature, PUBLIC_KEY, repository=REPOSITORY, channel="stable", now=datetime.now(UTC)
    )
    if verified.sha256 != result.manifest.sha256 or result.artifact not in verified.artifacts:
        raise UpdateError("Update metadata changed; check again before installation")
    job = target.parent / (".termana-update-" + uuid4().hex)
    from termana.terminal.private_recovery_files import PrivateDirectory

    with PrivateDirectory(job, create=True):
        pass
    secure_directory(job)
    _new_file(job / "manifest.json", raw)
    _new_file(job / "manifest.sig", signature)
    package = job / result.artifact.name
    downloader(verified, result.artifact, package, cancelled)
    with package.open("rb") as source:
        verify_package(source, result.artifact)
    if cancelled.is_set():
        raise UpdateError("Update cancelled; the installed app was not changed")
    if system == "macos":
        staged = extract_mac_package(package, job)
        validate_mac_bundle(staged)
    else:
        staged = package
        with staged.open("rb") as source:
            if source.read(2) != b"MZ":
                raise UpdateError("The downloaded package is not a Windows executable")
    old_executable = target / "Contents/MacOS/Termana" if system == "macos" else target
    with old_executable.open("rb") as source:
        old_sha = hashlib.file_digest(source, "sha256").hexdigest()
    plan = {
        "version": 1,
        "target": target.name,
        "staged": staged.name,
        "old_executable_sha256": old_sha,
        "system": system,
        "architecture": architecture,
    }
    _new_file(job / "plan.json", json.dumps(plan).encode("utf-8"))
    executable = staged / "Contents/MacOS/Termana" if system == "macos" else staged
    try:
        check_package_runtime(
            executable,
            job / "runtime-check.json",
            job,
            cancelled,
            expected_version=verified.version,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError(
            "The update cannot start on this computer; the installed app was not changed"
        ) from None
    if cancelled.is_set():
        raise UpdateError("Update cancelled; the installed app was not changed")
    return job


def replace_application(target, staged, rollback, *, rename=os.replace):
    """Two recoverable renames. Never remove the previous app or user data."""
    if rollback.exists() or rollback.is_symlink() or not target.exists():
        raise UpdateError("A previous installation needs recovery before replacing the app")
    deadline = time.monotonic() + 45
    while True:
        try:
            rename(target, rollback)
            break
        except PermissionError:
            # Windows' outer one-file bootloader can outlive the Python EOF pipe.
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    try:
        rename(staged, target)
    except BaseException:
        # Recovery itself may fail; leave the old app at its explicit rollback
        # path rather than claiming success or deleting the only working copy.
        rename(rollback, target)
        raise


def apply_job(job, *, restart=True):
    from termana.terminal.private_recovery_files import PrivateDirectory, PrivateFile

    secure_directory(job)
    with PrivateDirectory(job) as directory:
        with PrivateFile(
            directory, ".install.lock", create=not (job / ".install.lock").exists(), lease=True
        ):
            return _apply_job(job, restart=restart)


def _apply_job(job, *, restart=True):
    """Standalone helper; revalidate signed bytes and retain rollback on success."""
    secure_directory(job)
    if not re.fullmatch(r"[.]termana-update-[a-f0-9]{32}", job.name):
        raise UpdateError("Invalid update job")
    plan = json.loads((job / "plan.json").read_bytes())
    if (
        type(plan) is not dict
        or set(plan)
        != {"version", "target", "staged", "old_executable_sha256", "system", "architecture"}
        or plan["version"] != 1
    ):
        raise UpdateError("Invalid installation plan")
    for name in (plan["target"], plan["staged"]):
        if type(name) is not str or Path(name).name != name or name in {".", ".."}:
            raise UpdateError("Invalid installation target")
    system, architecture = native_target()
    if (plan["system"], plan["architecture"]) != (system, architecture):
        raise UpdateError("Installation target does not match this platform")
    manifest = read_signed_manifest(
        (job / "manifest.json").read_bytes(),
        (job / "manifest.sig").read_bytes(),
        PUBLIC_KEY,
        repository=REPOSITORY,
        channel="stable",
        now=datetime.now(UTC),
    )
    artifact = manifest.artifact_for(system, architecture)
    if artifact is None:
        raise UpdateError("No compatible installation package")
    target, staged = job.parent / plan["target"], job / plan["staged"]
    if (system == "macos" and target.name != "Termana.app") or (
        system == "windows" and target.suffix.lower() != ".exe"
    ):
        raise UpdateError("Unsupported desktop replacement target")
    secure_directory(target.parent)
    rollback = target.parent / (
        target.name + ".previous-" + job.name.removeprefix(".termana-update-")
    )
    journal = job / "result.json"
    if journal.exists():
        raw = journal.read_bytes()
        if len(raw) > 4096:
            raise UpdateError("Invalid installation recovery record")
        previous = json.loads(raw)
        if previous.get("status") == "prepared" and rollback.exists():
            # No child was launched before this journal phase completed. Restore
            # the known old application after a crash between the two renames.
            if target.exists():
                preserved = package_candidate = job / artifact.name
                if system == "macos":
                    preserved = job / ("interrupted-application-" + uuid4().hex)
                elif package_candidate.exists():
                    raise UpdateError("Ambiguous interrupted update; retain both apps for recovery")
                os.replace(target, preserved)
            os.replace(rollback, target)
        else:
            raise UpdateError(
                "This update was already attempted. Review its result before starting another installation"
            )
    package = job / artifact.name
    if package.is_symlink():
        raise UpdateError("Linked update packages are not allowed")
    with package.open("rb") as source:
        verify_package(source, artifact)
    if system == "macos":
        # Re-extract exact authenticated package into a new, exclusive directory;
        # don't execute an extracted tree which could have changed since download.
        fresh = job / ("verified-" + uuid4().hex)
        fresh.mkdir(mode=0o700)
        staged = extract_mac_package(package, fresh)
        validate_mac_bundle(staged)
    elif staged != package:
        raise UpdateError("Windows replacement is not the signed package")
    old_executable = target / "Contents/MacOS/Termana" if system == "macos" else target
    if target.is_symlink() or old_executable.is_symlink():
        raise UpdateError("Linked application targets are not supported")
    with old_executable.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != plan["old_executable_sha256"]:
            raise UpdateError("The installed application changed; prepare the update again")
    replace_file_atomically(
        journal, json.dumps({"status": "prepared", "rollback": rollback.name}).encode()
    )
    replace_application(target, staged, rollback)
    replace_file_atomically(
        journal, json.dumps({"status": "installed", "rollback": rollback.name}).encode()
    )
    if restart:
        command = [str(target / "Contents/MacOS/Termana")] if system == "macos" else [str(target)]
        try:
            process = subprocess.Popen(
                [*command, "--update-ack", str(job)],
                cwd=target.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=os.name != "nt",
            )
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if (job / "app-ready.json").exists():
                    if (job / "app-ready.json").read_bytes() != b'{"status":"ready"}':
                        raise UpdateError("The updated app did not confirm startup safely")
                    replace_file_atomically(
                        journal, json.dumps({"status": "ready", "rollback": rollback.name}).encode()
                    )
                    break
                if process.poll() is not None:
                    raise OSError("Updated process exited before readiness")
                time.sleep(0.1)
            else:
                # Do not kill a possibly interactive application or start two
                # copies against the same data. Keep old app and explicit status.
                replace_file_atomically(
                    journal,
                    json.dumps(
                        {"status": "startup-unconfirmed", "rollback": rollback.name}
                    ).encode(),
                )
                raise UpdateError(
                    "Startup was not confirmed. The previous app is retained; do not run two copies"
                )
        except OSError:
            failed = job / "failed-application"
            os.replace(target, failed)
            os.replace(rollback, target)
            restore_configuration_after_failed_start(job)
            replace_file_atomically(journal, b'{"status":"rolled-back"}')
            subprocess.Popen(
                command,
                cwd=target.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=os.name != "nt",
            )
            raise UpdateError(
                "The new app could not start; the previous app was restored"
            ) from None
    return rollback


def restore_configuration_after_failed_start(job):
    """Restore matching pre-update schemas only while owning the app-data lock.

    The existing journalled restore preserves any failed-version documents in
    configuration-backups. A busy app or ambiguous restore stops recovery rather
    than overwriting another process's settings or credentials.
    """
    from termana.data_directory import acquire_data_ownership
    from termana.paths import application_config_directory
    from termana.storage.configuration_backup import apply_pending_restore, stage_restore

    directory = application_config_directory()
    lock = acquire_data_ownership(directory)
    try:
        stage_restore(directory, job / "configuration-backup.zip")
        apply_pending_restore(directory)
    finally:
        lock.unlock()


def validate_ack_job(job):
    secure_directory(job)
    target = installed_target()
    if job.parent != target.parent or not job.name.startswith(".termana-update-"):
        raise UpdateError("Invalid update readiness directory")
    if (job / "app-ready.json").exists() or (job / "app-ready.json").is_symlink():
        raise UpdateError("Startup was already acknowledged")
    plan = json.loads((job / "plan.json").read_bytes())
    if plan.get("target") != target.name:
        raise UpdateError("Startup acknowledgement does not match the app")


def acknowledge_startup(job):
    # Called only after user-store ownership, migrations, MainWindow and a live
    # event-loop turn have succeeded. It never resets a bad configuration.
    validate_ack_job(job)
    _new_file(job / "app-ready.json", b'{"status":"ready"}')


def launch_installer(job):
    """Copy THIS trusted version as helper; the downloaded version need not know CLI."""
    if not getattr(sys, "frozen", False):
        raise UpdateError("Automatic installation is disabled for source launches")
    current = installed_target()
    if sys.platform == "darwin":
        helper = job / "Updater.app"
        shutil.copytree(current, helper, symlinks=True)
        executable = helper / "Contents/MacOS/Termana"
    else:
        executable = job / "updater.exe"
        shutil.copy2(sys.executable, executable)
    process = subprocess.Popen(
        [str(executable), "--apply-update-job", str(job)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=job,
        close_fds=True,
        start_new_session=os.name != "nt",
    )
    # The parent owns this process and its pipe until actual process exit.
    _HANDOFFS.append(process)
    return process


def main(job):
    try:
        # No PID polling/reuse race. No UI/app instance/config opened by helper.
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.ReadFile.argtypes = [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            ]
            kernel.ReadFile.restype = wintypes.BOOL
            handle = kernel.GetStdHandle(-10 & 0xFFFFFFFF)
            buffer, count = ctypes.create_string_buffer(1), wintypes.DWORD()
            read = kernel.ReadFile(handle, buffer, 1, ctypes.byref(count), None)
            if (not read and ctypes.get_last_error() != 109) or count.value:
                return 2
        elif os.read(0, 1) != b"":
            return 2
        apply_job(Path(job))
        return 0
    except Exception:
        try:
            replace_file_atomically(
                Path(job) / "error.json",
                b'{"status":"failed","message":"Installation failed; retain the update folder and previous app for recovery."}',
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
