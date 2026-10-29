"""Select the best terminal backend available on this platform."""

import sys
from collections.abc import Callable

from termana.terminal.backend import TerminalBackend
from termana.terminal.command import ShellCommand

type TerminalBackendFactory = Callable[[ShellCommand | None], TerminalBackend]


def create_terminal_backend(command: ShellCommand | None = None) -> TerminalBackend:
    """Create a controlling Unix PTY or an isolated Windows ConPTY backend."""
    if sys.platform != "win32":
        from termana.terminal.unix_pty_backend import UnixPtyTerminalBackend

        return UnixPtyTerminalBackend(command=command)
    from termana.terminal.conpty_backend import ConPtyTerminalBackend

    return ConPtyTerminalBackend(command=command)


def create_local_terminal_backend() -> TerminalBackend:
    """Create the default local shell backend."""
    return create_terminal_backend()
