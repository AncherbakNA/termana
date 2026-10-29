"""Build the Unix PTY helper's Python module invocation."""

import json
from typing import Final

from termana.helper_runtime import helper_command
from termana.terminal.command import ShellCommand

_HELPER_MODULE: Final = "termana.terminal.unix_pty_child"


def build_unix_pty_child_invocation(
    *,
    executable: str,
    slave_path: str,
    working_directory: str,
    command: ShellCommand,
) -> ShellCommand:
    """Use the same interpreter/environment as the GUI, without shell parsing."""
    if not executable:
        raise ValueError("The PTY helper executable cannot be empty.")

    helper_arguments = (
        "--slave-path",
        slave_path,
        "--working-directory",
        working_directory,
        "--command-json",
        json.dumps(
            [command.program, *command.arguments],
            ensure_ascii=False,
        ),
    )
    arguments = ("-u", "-m", _HELPER_MODULE, *helper_arguments)
    return helper_command(
        "unix-pty", ShellCommand(program=executable, arguments=arguments), helper_arguments
    )
