"""Fresh helper process that attaches a command to a Unix controlling PTY."""

import argparse
import json
import os
from collections.abc import Sequence
from typing import Final

from termana.credentials.askpass_helper import (
    ASKPASS_EXPECTED_PARENT_PID_ENV,
    ASKPASS_MODE_ENV,
    ASKPASS_MODE_VALUE,
)

READY_MESSAGE: Final = b"READY\n"
ERROR_PREFIX: Final = b"ERROR:"
TERMINAL_TYPE: Final = "xterm-256color"
CONTROL_OUTPUT_DESCRIPTOR: Final = 1
CONTROL_ERROR_DESCRIPTOR: Final = 2


def _parse_command(value: str) -> list[str]:
    """Decode and validate the command passed by the trusted parent process."""
    command = json.loads(value)
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or "\0" in part for part in command)
        or not command[0]
    ):
        raise ValueError("command must be a non-empty JSON list of strings")
    return command


def _write_error(file_descriptor: int, message: str) -> None:
    """Report a short startup failure without exposing a traceback."""
    safe_message = " ".join(message.splitlines())
    os.write(
        file_descriptor,
        ERROR_PREFIX + safe_message.encode("utf-8", errors="replace") + b"\n",
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """Attach to the slave PTY, then replace this helper with the command."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--slave-path", required=True)
    parser.add_argument("--working-directory", required=True)
    parser.add_argument("--command-json", required=True)
    options = parser.parse_args(arguments)

    # Keep the parent's control pipes separate before login_tty replaces fd 0/1/2.
    control_output_fd = os.dup(CONTROL_OUTPUT_DESCRIPTOR)
    control_error_fd = os.dup(CONTROL_ERROR_DESCRIPTOR)

    try:
        command = _parse_command(options.command_json)
        os.chdir(options.working_directory)
        slave_fd = os.open(options.slave_path, os.O_RDWR)
        os.login_tty(slave_fd)

        os.write(control_output_fd, READY_MESSAGE)
        os.close(control_output_fd)
        control_output_fd = -1

        environment = os.environ.copy()
        environment["TERM"] = TERMINAL_TYPE
        environment["COLORTERM"] = "truecolor"
        if environment.get(ASKPASS_MODE_ENV) == ASKPASS_MODE_VALUE:
            # The helper later proves whether OpenSSH invoked it directly or a
            # nested ProxyJump process inherited the non-secret broker locator.
            environment[ASKPASS_EXPECTED_PARENT_PID_ENV] = str(os.getpid())
        os.execvpe(command[0], command, environment)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _write_error(control_error_fd, str(error))
        return 127
    finally:
        if control_output_fd >= 0:
            os.close(control_output_fd)
        os.close(control_error_fd)

    return 127


if __name__ == "__main__":
    raise SystemExit(main())
