"""Unix pseudo-terminal backend integrated with Qt's event loop."""

import errno
import locale
import os
import pty
import signal
import sys
import termios
from pathlib import Path
from typing import Final

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QSocketNotifier, QTimer

from termana.terminal.backend import (
    MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN,
    TerminalBackend,
)
from termana.terminal.command import ShellCommand, default_shell_command
from termana.terminal.unix_pty_invocation import (
    build_unix_pty_child_invocation,
)

DEFAULT_COLUMNS: Final = 80
DEFAULT_ROWS: Final = 24
MAX_PENDING_INPUT_BYTES: Final = 1024 * 1024
IO_CHUNK_SIZE: Final = 64 * 1024
HANGUP_TIMEOUT_MS: Final = 250
TERMINATE_TIMEOUT_MS: Final = 250
READY_MESSAGE: Final = b"READY"
CONTROL_ERROR_PREFIX: Final = "ERROR:"


class UnixPtyTerminalBackend(TerminalBackend):
    """Run one command inside a real Unix controlling pseudo-terminal."""

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        command: ShellCommand | None = None,
        encoding: str | None = None,
        working_directory: str | None = None,
    ) -> None:
        """Configure a cold backend without creating descriptors or processes."""
        super().__init__(parent)

        self._command = command or default_shell_command()
        self._encoding = encoding or locale.getencoding() or "utf-8"
        self._working_directory = self._command.working_directory or working_directory or str(Path.home())
        self._columns = DEFAULT_COLUMNS
        self._rows = DEFAULT_ROWS

        self._master_fd: int | None = None
        self._slave_fd: int | None = None
        self._slave_path: str | None = None
        self._read_notifier: QSocketNotifier | None = None
        self._write_notifier: QSocketNotifier | None = None
        self._pending_input = bytearray()
        self._control_output = bytearray()
        self._ready = False
        self._closing = False
        self._completion_emitted = True
        self._pending_exit_code: int | None = None
        self._reported_startup_error = False
        self._shutdown_process_groups: set[int] = set()
        self._shutdown_session_id: int | None = None

        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.readyReadStandardOutput.connect(self._read_helper_control)
        self._process.readyReadStandardError.connect(self._read_helper_errors)
        self._process.finished.connect(self._handle_finished)
        self._process.errorOccurred.connect(self._handle_process_error)

        self._terminate_timer = QTimer(self)
        self._terminate_timer.setSingleShot(True)
        self._terminate_timer.timeout.connect(self._terminate_process)

        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill_process)

        self._drain_timer = QTimer(self)
        self._drain_timer.setSingleShot(True)
        self._drain_timer.timeout.connect(self._read_from_pty)

    @property
    def is_running(self) -> bool:
        """Keep ownership until both the child and its final output are finished."""
        return not self._completion_emitted

    @property
    def text_encoding(self) -> str:
        """Return the locale encoding inherited by the child command."""
        return self._encoding

    @property
    def echoes_input(self) -> bool:
        """A real terminal driver echoes canonical input by default."""
        return True

    @property
    def supports_direct_input(self) -> bool:
        """A Unix PTY accepts interactive key sequences directly."""
        return True

    @property
    def process_id(self) -> int | None:
        """Return the helper PID that remains stable when it execs the command."""
        process_id = int(self._process.processId())
        return process_id if process_id > 0 else None

    def start(self) -> None:
        """Create a PTY and launch the small login-tty helper process."""
        if self.is_running or self._master_fd is not None:
            return

        self._reset_run_state()
        try:
            self._open_pty()
        except (OSError, RuntimeError) as error:
            self.error_occurred.emit(f"Could not create a pseudo-terminal: {error}")
            self._complete(-1)
            return

        if self._slave_path is None:
            self.error_occurred.emit("Could not determine the pseudo-terminal path.")
            self._complete(-1)
            return

        helper_command = build_unix_pty_child_invocation(
            executable=sys.executable,
            slave_path=self._slave_path,
            working_directory=self._working_directory,
            command=self._command,
        )
        process_environment = QProcessEnvironment.systemEnvironment()
        for name, value in self._command.environment:
            process_environment.insert(name, value)
        self._process.setProcessEnvironment(process_environment)
        self._process.start(
            helper_command.program,
            list(helper_command.arguments),
        )

    def write(self, data: bytes) -> None:
        """Queue raw terminal input and write as much as the PTY accepts."""
        if (
            not self._ready
            or self._closing
            or self._master_fd is None
            or self._pending_exit_code is not None
        ):
            self.warning_occurred.emit("The pseudo-terminal is not ready for input.")
            return
        if len(self._pending_input) + len(data) > MAX_PENDING_INPUT_BYTES:
            self.warning_occurred.emit("The terminal input buffer is full.")
            return

        self._pending_input.extend(data)
        self._flush_pending_input()

    def resize(self, columns: int, rows: int) -> None:
        """Store and apply the terminal size in character cells."""
        if columns < 1 or rows < 1:
            raise ValueError("Terminal dimensions must be positive.")
        self._columns = columns
        self._rows = rows
        if self._master_fd is not None:
            self._apply_terminal_size(self._master_fd)

    def close(self) -> None:
        """Begin asynchronous process-group shutdown without blocking Qt."""
        if self._closing:
            return
        if self._completion_emitted:
            self._dispose_pty()
            return

        self._closing = True
        self._ready = False
        self._pending_input.clear()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            # The process may have exited while several bounded output slices
            # still belong to this session. Closing/reopening must not erase them.
            if self._pending_exit_code is not None:
                self._drain_timer.start(0)
            return
        self._shutdown_process_groups = self._collect_process_groups()
        self._signal_process_groups(signal.SIGHUP)
        self._dispose_pty()
        self._terminate_timer.start(HANGUP_TIMEOUT_MS)

    def _reset_run_state(self) -> None:
        """Reset all per-run flags before allocating operating-system resources."""
        self._completion_emitted = False
        self._pending_exit_code = None
        self._closing = False
        self._ready = False
        self._reported_startup_error = False
        self._pending_input.clear()
        self._control_output.clear()
        self._shutdown_process_groups.clear()
        self._shutdown_session_id = None
        self._drain_timer.stop()

    def _open_pty(self) -> None:
        """Allocate non-blocking master/slave descriptors and Qt notifiers."""
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._slave_fd = slave_fd
        try:
            self._slave_path = os.ttyname(slave_fd)
            os.set_blocking(master_fd, False)
            self._apply_terminal_size(slave_fd)

            self._read_notifier = QSocketNotifier(
                master_fd,
                QSocketNotifier.Type.Read,
                self,
            )
            self._read_notifier.setEnabled(False)
            self._read_notifier.activated.connect(self._read_from_pty)

            self._write_notifier = QSocketNotifier(
                master_fd,
                QSocketNotifier.Type.Write,
                self,
            )
            self._write_notifier.setEnabled(False)
            self._write_notifier.activated.connect(self._flush_pending_input)
        except (OSError, RuntimeError):
            self._dispose_pty()
            raise

    def _apply_terminal_size(self, file_descriptor: int) -> None:
        """Apply rows first and columns second, as required by termios."""
        try:
            termios.tcsetwinsize(file_descriptor, (self._rows, self._columns))
        except OSError as error:
            if not self._closing:
                self.warning_occurred.emit(f"Could not resize the pseudo-terminal: {error}")

    def _read_helper_control(self) -> None:
        """Wait for login_tty completion before exposing the backend as started."""
        self._control_output.extend(bytes(self._process.readAllStandardOutput()))
        while b"\n" in self._control_output:
            raw_line, _, remainder = self._control_output.partition(b"\n")
            self._control_output = bytearray(remainder)
            if raw_line != READY_MESSAGE or self._ready:
                continue

            self._ready = True
            # Keep the parent's slave until final drain. On macOS the last slave
            # close can discard unread master output if a short-lived command
            # exited before Qt handled READY. The guard is non-inheritable and
            # never used for application input; explicit close releases it too.
            self._process.closeWriteChannel()
            if self._read_notifier is not None:
                self._read_notifier.setEnabled(True)
            if self._closing:
                self.close()
                return
            self.started.emit()

    def _read_helper_errors(self) -> None:
        """Surface helper startup errors separately from terminal output."""
        raw_error = bytes(self._process.readAllStandardError())
        if not raw_error or self._closing:
            return
        message = raw_error.decode("utf-8", errors="replace").strip()
        if message.startswith(CONTROL_ERROR_PREFIX):
            message = message.removeprefix(CONTROL_ERROR_PREFIX).strip()
        self._reported_startup_error = True
        self.error_occurred.emit(f"Could not start the pseudo-terminal: {message}")

    def _read_from_pty(self, *_signal_arguments: object) -> None:
        """Read one small output slice so Qt can service input between slices."""
        if self._master_fd is None:
            self._complete_pending_exit()
            return

        master_fd = self._master_fd
        output = bytearray()
        reached_end = False
        while len(output) < MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN:
            try:
                remaining = MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN - len(output)
                data = os.read(master_fd, min(IO_CHUNK_SIZE, remaining))
            except BlockingIOError:
                reached_end = True
                break
            except OSError as error:
                if error.errno not in {errno.EBADF, errno.EIO} and not self._closing:
                    self.error_occurred.emit(f"Could not read terminal output: {error}")
                self._disable_read_notifier()
                reached_end = True
                break

            if not data:
                self._disable_read_notifier()
                reached_end = True
                break
            output.extend(data)

        if output:
            self.data_received.emit(bytes(output))
        if self._master_fd != master_fd:
            return
        if reached_end:
            self._complete_pending_exit()
        elif self._pending_exit_code is not None:
            # With the guard slave open an exact-sized final slice may be followed
            # by EAGAIN rather than EOF, so do not rely on another notifier wakeup.
            self._drain_timer.start(0)

    def _flush_pending_input(self, *_signal_arguments: object) -> None:
        """Handle partial non-blocking writes and enable notification only if needed."""
        if self._master_fd is None:
            self._pending_input.clear()
            self._disable_write_notifier()
            return

        while self._pending_input:
            chunk = bytes(self._pending_input[:IO_CHUNK_SIZE])
            try:
                bytes_written = os.write(self._master_fd, chunk)
            except BlockingIOError:
                if self._write_notifier is not None:
                    self._write_notifier.setEnabled(True)
                return
            except OSError as error:
                self._pending_input.clear()
                self._disable_write_notifier()
                if not self._closing:
                    self.error_occurred.emit(f"Could not write terminal input: {error}")
                return

            del self._pending_input[:bytes_written]

        self._disable_write_notifier()

    def _handle_finished(
        self,
        exit_code: int,
        _exit_status: QProcess.ExitStatus,
    ) -> None:
        """Remember completion and publish it only after final PTY output."""
        if self._completion_emitted:
            return
        self._pending_exit_code = exit_code
        self._read_helper_control()
        self._read_helper_errors()
        if not self._ready and not self._closing and not self._reported_startup_error:
            self.error_occurred.emit("The pseudo-terminal helper exited before startup.")
        self._read_from_pty()

    def _handle_process_error(self, error: QProcess.ProcessError) -> None:
        """Separate helper-ending process errors from recoverable I/O warnings."""
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

    def _complete(self, exit_code: int) -> None:
        """Release descriptors and publish one completion signal."""
        if self._completion_emitted:
            return
        self._completion_emitted = True
        self._terminate_timer.stop()
        self._kill_timer.stop()
        self._drain_timer.stop()
        self._dispose_pty()
        self._pending_exit_code = None
        self._closing = False
        self._ready = False
        self.finished.emit(exit_code)

    def _complete_pending_exit(self) -> None:
        """Complete a stopped child after its readable PTY bytes are consumed."""
        if self._pending_exit_code is not None:
            self._complete(self._pending_exit_code)

    def _collect_process_groups(self) -> set[int]:
        """Collect only child groups from this PTY session, never the GUI group."""
        process_id = int(self._process.processId())
        if process_id <= 1:
            return set()

        try:
            session_id = os.getsid(process_id)
        except ProcessLookupError:
            return set()
        self._shutdown_session_id = session_id

        candidates: set[int] = set()
        try:
            candidates.add(os.getpgid(process_id))
        except ProcessLookupError:
            pass
        if self._master_fd is not None:
            try:
                candidates.add(os.tcgetpgrp(self._master_fd))
            except OSError:
                pass

        gui_process_group = os.getpgrp()
        safe_groups: set[int] = set()
        for process_group in candidates:
            if process_group <= 1 or process_group == gui_process_group:
                continue
            try:
                if os.getsid(process_group) == session_id:
                    safe_groups.add(process_group)
            except ProcessLookupError:
                continue
        return safe_groups

    def _signal_process_groups(self, selected_signal: signal.Signals) -> None:
        """Send a signal only to the previously validated PTY process groups."""
        gui_process_group = os.getpgrp()
        for process_group in self._shutdown_process_groups:
            if process_group <= 1 or process_group == gui_process_group:
                continue
            try:
                if (
                    self._shutdown_session_id is None
                    or os.getsid(process_group) != self._shutdown_session_id
                ):
                    continue
                os.killpg(process_group, selected_signal)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                if not self._closing:
                    self.error_occurred.emit(f"Could not stop terminal process: {error}")

    def _terminate_process(self) -> None:
        """Escalate from terminal hangup to SIGTERM."""
        if not self.is_running:
            return
        self._signal_process_groups(signal.SIGTERM)
        self._process.terminate()
        self._kill_timer.start(TERMINATE_TIMEOUT_MS)

    def _kill_process(self) -> None:
        """Force-stop process groups and the tracked shell as the final fallback."""
        if not self.is_running:
            return
        self._signal_process_groups(signal.SIGKILL)
        self._process.kill()

    def _disable_read_notifier(self) -> None:
        """Stop read callbacks before the master descriptor is closed."""
        if self._read_notifier is not None:
            self._read_notifier.setEnabled(False)

    def _disable_write_notifier(self) -> None:
        """Prevent an always-writable descriptor from spinning the event loop."""
        if self._write_notifier is not None:
            self._write_notifier.setEnabled(False)

    def _close_slave_descriptor(self) -> None:
        """Release the parent's output-preserving guard during final disposal."""
        if self._slave_fd is None:
            return
        os.close(self._slave_fd)
        self._slave_fd = None

    def _dispose_pty(self) -> None:
        """Disable notifiers and idempotently close both PTY descriptors."""
        self._disable_read_notifier()
        self._disable_write_notifier()
        for notifier_name in ("_read_notifier", "_write_notifier"):
            notifier = getattr(self, notifier_name)
            if notifier is not None:
                notifier.deleteLater()
                setattr(self, notifier_name, None)

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        try:
            self._close_slave_descriptor()
        except OSError:
            self._slave_fd = None
        self._slave_path = None
        self._pending_input.clear()
