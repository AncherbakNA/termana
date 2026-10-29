"""Process commands shared by terminal backend implementations."""

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final

_ENVIRONMENT_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class ShellCommand:
    """A program and its already-separated arguments, never a shell string."""

    program: str
    arguments: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    working_directory: str | None = None

    def __post_init__(self) -> None:
        """Freeze and validate a small child-only environment overlay."""
        if self.working_directory is not None and (
            type(self.working_directory) is not str
            or not os.path.isabs(self.working_directory)
            or any(c in self.working_directory for c in '\0\r\n')
        ):
            raise ValueError('Command working directory must be an absolute path')
        selected_environment = tuple(self.environment)
        normalized_names: set[str] = set()
        for item in selected_environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("Command environment entries must be name-value pairs")
            name, value = item
            if not isinstance(name, str) or _ENVIRONMENT_NAME.fullmatch(name) is None:
                raise ValueError("Command environment contains an invalid variable name")
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("Command environment values must be NUL-free text")
            normalized_name = name.casefold() if sys.platform == "win32" else name
            if normalized_name in normalized_names:
                raise ValueError("Command environment contains duplicate variable names")
            normalized_names.add(normalized_name)
        object.__setattr__(self, "environment", selected_environment)


def default_shell_command(
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ShellCommand:
    """Choose the user's shell on Unix and the command processor on Windows."""
    selected_platform = platform_name or sys.platform
    selected_environment = os.environ if environment is None else environment

    if selected_platform == "win32":
        fallback = "cmd.exe"
        if system_root := selected_environment.get("SystemRoot"):
            fallback = str(PureWindowsPath(system_root) / "System32" / "cmd.exe")
        return ShellCommand(
            program=selected_environment.get("COMSPEC", fallback),
            arguments=("/Q",),
        )

    shell = selected_environment.get("SHELL")
    if shell and PurePosixPath(shell).is_absolute():
        return ShellCommand(program=shell)

    return ShellCommand(program="/bin/sh")
