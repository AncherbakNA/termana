"""Fixed helper roles for bundled applications; never a general Python runner."""

import os
import sys
from pathlib import Path

from termana.terminal.command import ShellCommand

HELPER_FLAG = "--termana-helper"
HELPER_ROLES = frozenset(
    {"unix-pty", "conpty", "system-sftp-response", "system-sftp-child", "probe"}
)


def is_bundled() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundled_helper_path() -> Path:
    """Use only the build-owned binary, never PATH/cwd/environment overrides."""
    root = Path(sys._MEIPASS)  # Set by the frozen bootloader, not an environment variable.
    path = root / ("termana-runtime.exe" if sys.platform == "win32" else "termana-runtime")
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("The bundled terminal runtime is missing or invalid.")
    return path


def helper_command(
    role: str, source: ShellCommand, arguments: tuple[str, ...] = ()
) -> ShellCommand:
    """Source invocations stay unchanged; a bundle uses its fixed-role runtime."""
    if role not in HELPER_ROLES:
        raise ValueError("Unknown terminal helper role")
    if not is_bundled():
        return source
    return ShellCommand(str(bundled_helper_path()), (HELPER_FLAG, role, *arguments))


def main() -> int:
    # OpenSSH invokes its AskPass executable directly with a single prompt.
    from termana.credentials.askpass_helper import is_askpass_invocation
    from termana.credentials.askpass_helper import main as askpass_main

    if len(sys.argv) < 2 or sys.argv[1] != HELPER_FLAG:
        return askpass_main() if is_askpass_invocation() else 2
    if len(sys.argv) < 3 or sys.argv[2] not in HELPER_ROLES:
        return 2
    role = sys.argv[2]
    sys.argv = [sys.argv[0], *sys.argv[3:]]
    if role == "probe":
        if len(sys.argv) != 1:
            return 2
        print("TERMANA_RUNTIME_OK", flush=True)
        return 0
    if role == "unix-pty" and sys.platform != "win32":
        from termana.terminal.unix_pty_child import main as run
    elif role == "conpty" and sys.platform == "win32":
        from termana.terminal.conpty_child import main as run
    elif role == "system-sftp-response":
        from termana.terminal.system_sftp_askpass import main as run
    elif role == "system-sftp-child" and sys.platform == "win32":
        from termana.terminal.system_sftp_child import main as run
    else:
        return 2
    return run()
