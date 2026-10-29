"""Common interface for replaceable terminal backends."""

from typing import Final

from PySide6.QtCore import QObject, Signal

MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN: Final = 4 * 1024
PROCESS_EXIT_SIGNAL_TYPE: Final = "qlonglong"


class TerminalBackend(QObject):
    """Expose process lifecycle and raw terminal data to widgets."""

    data_received = Signal(bytes)
    started = Signal()
    # Preserve unsigned Windows DWORD exit codes and negative backend failures.
    # Qt's ordinary int truncates values above 0x7fffffff before Python receives them.
    finished = Signal(PROCESS_EXIT_SIGNAL_TYPE)
    warning_occurred = Signal(str)
    error_occurred = Signal(str)

    @property
    def uses_legacy_console_colors(self) -> bool:
        """Whether ordinary console ANSI black is the themed base surface."""
        return False

    @property
    def is_running(self) -> bool:
        """Return whether the backend currently owns a running process."""
        raise NotImplementedError

    @property
    def text_encoding(self) -> str:
        """Return the encoding used for user-entered text."""
        raise NotImplementedError

    @property
    def echoes_input(self) -> bool:
        """Return whether the terminal driver echoes written input."""
        raise NotImplementedError

    @property
    def supports_direct_input(self) -> bool:
        """Return whether individual terminal key sequences are meaningful."""
        raise NotImplementedError

    @property
    def process_id(self) -> int | None:
        """Return the stable child PID when this backend directly owns one."""
        return None

    def start(self) -> None:
        """Start the terminal process."""
        raise NotImplementedError

    def prepare_input(self, cursor_column: int, columns: int) -> None:
        """Receive display context immediately before direct user input."""

    def write(self, data: bytes) -> None:
        """Write raw bytes to the terminal process."""
        raise NotImplementedError

    def resize(self, columns: int, rows: int) -> None:
        """Update the terminal's character-cell dimensions."""
        raise NotImplementedError

    def close(self) -> None:
        """Begin shutdown; emit finished after resources are safe to release."""
        raise NotImplementedError
