"""Fallback cross-platform shell bridge implemented with QProcess pipes."""

import codecs
import locale
import sys
from pathlib import Path
from typing import Final

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer

from termana.terminal.backend import (
    MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN,
    TerminalBackend,
)
from termana.terminal.command import ShellCommand, default_shell_command

EOF_SHUTDOWN_TIMEOUT_MS: Final = 250
TERMINATE_SHUTDOWN_TIMEOUT_MS: Final = 250


def default_process_encoding(
    platform_name: str | None = None,
    *,
    windows_oem_code_page: int | None = None,
    locale_encoding: str | None = None,
) -> str:
    """Choose the byte encoding used by the platform command processor."""
    selected_platform = platform_name or sys.platform
    fallback = locale_encoding or locale.getencoding() or "utf-8"
    if selected_platform != "win32":
        return fallback

    code_page = windows_oem_code_page
    if code_page is None:
        try:
            import ctypes

            code_page = int(ctypes.windll.kernel32.GetOEMCP())
        except (AttributeError, OSError, TypeError, ValueError):
            return fallback
    if isinstance(code_page, bool) or code_page < 1:
        return fallback

    encoding = f"cp{code_page}"
    try:
        codecs.lookup(encoding)
    except LookupError:
        return fallback
    return encoding


class QProcessTerminalBackend(TerminalBackend):
    """Exchange plain text with a shell through standard input and output."""

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        command: ShellCommand | None = None,
        encoding: str | None = None,
        working_directory: str | None = None,
    ) -> None:
        """Configure a process without starting it."""
        super().__init__(parent)

        self._command = command or default_shell_command()
        self._encoding = encoding or default_process_encoding()
        self._closing = False
        self._completion_emitted = True
        self._pending_exit_code: int | None = None

        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.setWorkingDirectory(self._command.working_directory or working_directory or str(Path.home()))

        process_environment = QProcessEnvironment.systemEnvironment()
        if sys.platform != "win32":
            process_environment.insert("TERM", "dumb")
        for name, value in self._command.environment:
            process_environment.insert(name, value)
        self._process.setProcessEnvironment(process_environment)

        self._terminate_timer = QTimer(self)
        self._terminate_timer.setSingleShot(True)
        self._terminate_timer.timeout.connect(self._terminate_process)

        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill_process)

        self._output_timer = QTimer(self)
        self._output_timer.setSingleShot(True)
        self._output_timer.timeout.connect(self._read_output_chunk)

        self._process.started.connect(self._handle_started)
        self._process.readyReadStandardOutput.connect(self._schedule_output_read)
        self._process.finished.connect(self._handle_finished)
        self._process.errorOccurred.connect(self._handle_error)

    @property
    def is_running(self) -> bool:
        """Own the session until the child and all buffered output are finished."""
        return not self._completion_emitted

    @property
    def text_encoding(self) -> str:
        """Return the byte encoding used by the selected command processor."""
        return self._encoding

    @property
    def echoes_input(self) -> bool:
        """Piped shells do not provide reliable terminal-driver echo."""
        return False

    @property
    def supports_direct_input(self) -> bool:
        """Ordinary process pipes need the temporary line-input fallback."""
        return False

    @property
    def process_id(self) -> int | None:
        """Return the QProcess child PID while the fallback command is alive."""
        process_id = int(self._process.processId())
        return process_id if process_id > 0 else None

    def start(self) -> None:
        """Start the configured shell process once it is idle."""
        if self.is_running:
            return

        self._closing = False
        self._completion_emitted = False
        self._pending_exit_code = None
        self._output_timer.stop()
        self._process.start(self._command.program, list(self._command.arguments))

    def write(self, data: bytes) -> None:
        """Write bytes to the process standard input."""
        if self._process.state() != QProcess.ProcessState.Running or self._closing:
            self.warning_occurred.emit("The shell process is not ready for input.")
            return

        bytes_written = self._process.write(data)
        if bytes_written < 0:
            self.warning_occurred.emit("Could not write to the shell process.")

    def resize(self, columns: int, rows: int) -> None:
        """Accept the common API; ordinary process pipes have no screen size."""
        if columns < 1 or rows < 1:
            raise ValueError("Terminal dimensions must be positive.")

    def close(self) -> None:
        """Begin a non-blocking EOF, terminate, then kill shutdown sequence."""
        if not self.is_running or self._closing:
            return

        self._closing = True
        if self._process.state() == QProcess.ProcessState.NotRunning:
            # A stopped child may still have bounded output slices awaiting Qt.
            # Preserve them without sending EOF or starting termination timers.
            self._schedule_output_read()
            return
        self._terminate_timer.start(EOF_SHUTDOWN_TIMEOUT_MS)
        self._process.closeWriteChannel()

    def _handle_started(self) -> None:
        """Publish startup unless shutdown was requested while starting."""
        if self._closing:
            self._process.closeWriteChannel()
            return
        self.started.emit()

    def _schedule_output_read(self) -> None:
        """Queue one bounded output read on the next event-loop turn."""
        if not self._output_timer.isActive():
            self._output_timer.start(0)

    def _read_output_chunk(self) -> None:
        """Publish at most one parser-sized output chunk per Qt turn."""
        if self._completion_emitted:
            return
        output = bytes(self._process.read(MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN))
        if output:
            self.data_received.emit(output)
        if self._process.bytesAvailable() > 0:
            self._output_timer.start(0)
        elif self._pending_exit_code is not None:
            self._complete(self._pending_exit_code)

    def _handle_finished(
        self,
        exit_code: int,
        _exit_status: QProcess.ExitStatus,
    ) -> None:
        """Wait for buffered output slices before publishing completion."""
        if self._completion_emitted:
            return
        self._pending_exit_code = exit_code
        self._schedule_output_read()

    def _complete(self, exit_code: int) -> None:
        """Complete one process run exactly once."""
        if self._completion_emitted:
            return

        self._completion_emitted = True
        self._pending_exit_code = None
        self._output_timer.stop()
        self._terminate_timer.stop()
        self._kill_timer.stop()
        self._closing = False
        self.finished.emit(exit_code)

    def _handle_error(self, error: QProcess.ProcessError) -> None:
        """Separate terminal-ending process errors from recoverable I/O warnings."""
        if not self._closing:
            if error in {
                QProcess.ProcessError.FailedToStart,
                QProcess.ProcessError.Crashed,
            }:
                self.error_occurred.emit(self._process.errorString())
            else:
                self.warning_occurred.emit(self._process.errorString())
        if error == QProcess.ProcessError.FailedToStart:
            self._complete(-1)

    def _terminate_process(self) -> None:
        """Ask a process that ignored EOF to stop, without blocking the GUI."""
        if not self.is_running or self._process.state() == QProcess.ProcessState.NotRunning:
            return
        self._process.terminate()
        self._kill_timer.start(TERMINATE_SHUTDOWN_TIMEOUT_MS)

    def _kill_process(self) -> None:
        """Force-stop a process that ignored the graceful shutdown requests."""
        if self.is_running and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
