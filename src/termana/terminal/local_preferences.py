"""Local-only executable discovery and literal command construction; no shell strings."""

import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path, PureWindowsPath

from termana.models.settings import LocalTerminalSettings
from termana.terminal.command import ShellCommand, default_shell_command


def available_local_shells() -> tuple[tuple[str, str], ...]:
    """Find installed choices without executing them or sourcing shell startup files."""
    names = ("pwsh", "powershell", "cmd", "bash", "zsh", "fish", "sh", "ksh", "dash")
    candidates = [(name, shutil.which(name)) for name in names]
    if sys.platform != "win32":
        candidates += [(Path(path).name, path) for path in ("/bin/bash", "/bin/zsh", "/bin/sh")]
    result, seen = [], set()
    for label, path in candidates:
        if not path or not Path(path).is_file() or not os.access(path, os.X_OK):
            continue
        absolute = os.path.abspath(path)
        identity = os.path.normcase(os.path.realpath(absolute))
        if identity not in seen:
            result.append((label, absolute))
            seen.add(identity)
    return tuple(result)


def local_shell_command(settings: LocalTerminalSettings) -> ShellCommand | None:
    """Validate explicit choices on this host. Never silently fall back after failure."""
    if type(settings) is not LocalTerminalSettings:
        raise TypeError("Expected LocalTerminalSettings")
    if settings == LocalTerminalSettings():
        return None  # Preserve the established SHELL/COMSPEC and home defaults.
    directory = settings.directory or str(Path.home())
    if not os.path.isabs(directory) or not Path(directory).is_dir():
        raise ValueError(
            "Local start directory is unavailable. Choose an existing folder in Settings."
        )
    command = default_shell_command()
    if settings.shell is not None:
        program = settings.shell
        if (
            not os.path.isabs(program)
            or not Path(program).is_file()
            or not os.access(program, os.X_OK)
        ):
            raise ValueError(
                "Local shell is unavailable. Choose an installed executable in Settings."
            )
        name = (
            PureWindowsPath(program).name if sys.platform == "win32" else Path(program).name
        ).casefold()
        if sys.platform == "win32" and Path(program).suffix.casefold() not in {".exe", ".com"}:
            raise ValueError(
                "Choose a shell executable (.exe or .com), not a script or command line."
            )
        arguments = (
            ("/Q",)
            if name in {"cmd", "cmd.exe"}
            else (
                ("-NoLogo",) if name in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"} else ()
            )
        )
        command = ShellCommand(program, arguments)
    return replace(command, working_directory=directory)
