"""Event-loop driven Windows terminal using an isolated native ConPTY helper."""

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer

from termana.terminal.backend import MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN, TerminalBackend
from termana.terminal.command import ShellCommand, default_shell_command
from termana.terminal.conpty_protocol import (
    HEADER,
    MAX_FRAME_BYTES,
    SIZE,
    frame,
    helper_invocation,
    launch_frame,
    validate_size,
)

MAX_CONTROL_BYTES = 4096
SHUTDOWN_TIMEOUT_MS = 1000
STARTUP_TIMEOUT_MS = 10000
RESIZE_RETRY_TIMEOUT_MS = 30
MAX_CONTROL_INTEGER = 0xFFFFFFFF  # Windows PID/exit code are unsigned DWORDs.


class ConPtyTerminalBackend(TerminalBackend):
    """UTF-8/VT direct input, resize and bounded GUI delivery. No native GUI-thread calls."""

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        command: ShellCommand | None = None,
        working_directory: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._command = command or default_shell_command()
        self._cwd = self._command.working_directory or working_directory or str(Path.home())
        self._columns, self._rows = 80, 24
        self._pid: int | None = None
        self._generation = 0
        self._reading_control = False
        self._closing = False
        self._completed = True
        self._pending_exit: int | None = None
        self._helper_result: tuple[int, QProcess.ExitStatus] | None = None
        self._reported_exit: int | None = None
        self._control = bytearray()
        self._output = bytearray()
        self._input_pending = 0
        self._error_reported = False
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.started.connect(self._launch)
        self._process.readyReadStandardOutput.connect(self._schedule_output)
        self._process.readyReadStandardError.connect(self._read_control)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error)
        self._output_timer = QTimer(self)
        self._output_timer.setSingleShot(True)
        self._output_timer.timeout.connect(self._read_output)
        self._control_timer = QTimer(self)
        self._control_timer.setSingleShot(True)
        self._control_timer.timeout.connect(self._read_control)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._startup_timeout)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._process.kill)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._send_resize)

    @property
    def is_running(self) -> bool:
        return not self._completed

    @property
    def text_encoding(self) -> str:
        return "utf-8"

    @property
    def echoes_input(self) -> bool:
        return True

    @property
    def supports_direct_input(self) -> bool:
        return True

    @property
    def process_id(self) -> int | None:
        return self._pid

    def start(self) -> None:
        if self.is_running:
            return
        self._generation += 1
        self._completed = False
        self._closing = False
        self._pid = None
        self._pending_exit = self._reported_exit = None
        self._helper_result = None
        self._control.clear()
        self._output.clear()
        self._input_pending = 0
        self._error_reported = False
        if sys.platform != "win32":
            self._fail("ConPTY is available only on Windows 10 version 1809 or newer.")
            self._complete(-1)
            return
        try:
            launch_frame(self._command, self._cwd, self._columns, self._rows)
        except (ValueError, TypeError, UnicodeError):
            self._fail("Invalid Windows terminal launch settings.")
            self._complete(-1)
            return
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("TERM", "xterm-256color")
        environment.insert("COLORTERM", "truecolor")
        for name, value in self._command.environment:
            environment.insert(name, value)
        self._process.setProcessEnvironment(environment)
        self._process.setWorkingDirectory(self._cwd)
        helper = helper_invocation()
        self._startup_timer.start(STARTUP_TIMEOUT_MS)
        self._process.start(helper.program, list(helper.arguments))

    def _launch(self) -> None:
        if self._closing:
            self._process.closeWriteChannel()
            return
        self._process.write(launch_frame(self._command, self._cwd, self._columns, self._rows))

    def write(self, data: bytes) -> None:
        if not self._pid or self._closing or not self.is_running:
            self.warning_occurred.emit("The Windows terminal is not ready for input.")
            return
        packet_size = len(data) + HEADER.size
        if (
            self._input_pending + len(data) > MAX_FRAME_BYTES
            or self._process.bytesToWrite() + packet_size > MAX_FRAME_BYTES
        ):
            self.warning_occurred.emit(
                "Terminal input is busy; this input was not sent. Try a smaller paste."
            )
            return
        self._input_pending += len(data)
        if self._process.write(frame(b"I", data)) < 0:
            self._fail("Could not send input to the Windows terminal.")
            self.close()

    def resize(self, columns: int, rows: int) -> None:
        validate_size(columns, rows)
        self._columns, self._rows = columns, rows
        if self.is_running and self._pid and not self._closing:
            self._resize_timer.start(0)

    def _send_resize(self) -> None:
        if self._pid and self.is_running and not self._closing:
            packet_size = HEADER.size + SIZE.size
            if self._process.bytesToWrite() > MAX_FRAME_BYTES - packet_size:
                self._resize_timer.start(RESIZE_RETRY_TIMEOUT_MS)
                return
            if self._process.write(frame(b"R", SIZE.pack(self._columns, self._rows))) < 0:
                self._fail("Could not resize the Windows terminal.")
                self.close()

    def close(self) -> None:
        if self._completed or self._closing:
            return
        self._closing = True
        self._startup_timer.stop()
        self._resize_timer.stop()
        self._process.closeWriteChannel()
        # A stalled native read/write/ClosePseudoConsole cannot block Qt. Killing
        # the private helper closes its job handle and terminates its owned tree.
        self._kill_timer.start(SHUTDOWN_TIMEOUT_MS)

    def _read_control(self) -> None:
        if self._reading_control:
            # Signal callbacks can enter a nested Qt event loop. Keep this
            # batch's ordering, especially READY followed by the real EXIT.
            self._control_timer.start(0)
            return
        self._reading_control = True
        self._control_timer.stop()
        try:
            self._pump_control()
        finally:
            self._reading_control = False

    def _pump_control(self) -> None:
        # A large batch of short DRAIN lines is valid. Bound each Qt turn, not
        # the whole batch, and restore stdout before the output pump can run.
        channel = self._process.readChannel()
        self._process.setReadChannel(QProcess.ProcessChannel.StandardError)
        try:
            self._control.extend(bytes(self._process.read(MAX_CONTROL_BYTES)))
            more = bool(self._process.bytesAvailable())
        finally:
            self._process.setReadChannel(channel)
        lines = self._control.split(b"\n")
        self._control = lines.pop()
        if len(self._control) > MAX_CONTROL_BYTES or any(
            len(line) > MAX_CONTROL_BYTES for line in lines
        ):
            self._fail("The Windows terminal helper returned invalid control data.")
            self._control.clear()
            self.close()
        else:
            for line in lines:
                self._handle_control(bytes(line))
        if more or self._control_timer.isActive():
            self._control_timer.start(0)
        elif self._helper_result is not None:
            code, status = self._helper_result
            self._helper_result = None
            self._finish_after_control(code, status)

    def _handle_control(self, line: bytes) -> None:
        if line.startswith(b"DRAIN:") and self._pid:
            value = line[6:]
            if value.isdigit() and len(value) <= 7 and 0 < int(value) <= self._input_pending:
                self._input_pending -= int(value)
                return
        elif line.startswith(b"READY:") and self._pid is None and not self._completed:
            value = line[6:]
            if value.isdigit() and 0 < len(value) <= 10 and 0 < int(value) <= MAX_CONTROL_INTEGER:
                self._pid = int(value)
                self._startup_timer.stop()
                if not self._closing:
                    self._send_resize()
                    self.started.emit()
                return
        elif line.startswith(b"EXIT:") and self._pid and self._reported_exit is None:
            value = line[5:]
            if value.isdigit() and 0 < len(value) <= 10 and int(value) <= MAX_CONTROL_INTEGER:
                self._reported_exit = int(value)
                # Bound teardown even when an inherited stdin read is still blocked.
                self._kill_timer.start(SHUTDOWN_TIMEOUT_MS)
                return
        elif line.startswith(b"ERROR:"):
            self._fail(
                "Windows terminal startup or I/O failed. Check Windows ConPTY support and the selected program."
            )
            self.close()
            return
        self._fail("The Windows terminal helper returned invalid control data.")
        self.close()

    def _schedule_output(self) -> None:
        if not self._output_timer.isActive():
            self._output_timer.start(0)

    def _read_output(self) -> None:
        generation = self._generation
        # The helper permits one frame in flight and waits for our ACK before
        # reading ConPTY again. Qt cannot accumulate an unlimited output stream.
        limit = MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN + HEADER.size
        self._output.extend(bytes(self._process.read(limit - len(self._output))))
        if len(self._output) >= HEADER.size:
            kind, size = HEADER.unpack(self._output[: HEADER.size])
            if kind != b"O" or not 0 < size <= MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN:
                self._fail("The Windows terminal helper returned an invalid output frame.")
                self._output.clear()
                self.close()
            elif len(self._output) >= HEADER.size + size:
                data = bytes(self._output[HEADER.size : HEADER.size + size])
                del self._output[: HEADER.size + size]
                self.data_received.emit(data)
                # A callback may close, finish and restart this same backend in
                # a nested event loop. Do not send an old ACK into the new run.
                if self._completed or generation != self._generation:
                    return
                if not self._closing and self._process.state() == QProcess.ProcessState.Running:
                    self._process.write(frame(b"A"))
        if self._process.bytesAvailable():
            self._output_timer.start(0)
        elif self._pending_exit is not None:
            if self._output:
                self._fail("The Windows terminal output ended in an incomplete frame.")
                self._output.clear()
                self._pending_exit = -1
            self._complete(self._pending_exit)

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        # The last control batch may include the client's actual exit code.
        # Finish only after it has been read, including timer-deferred chunks.
        self._helper_result = (code, status)
        self._read_control()

    def _finish_after_control(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._control:
            self._fail("The Windows terminal helper returned incomplete control data.")
            self._control.clear()
        if (
            self._reported_exit is not None
            and status == QProcess.ExitStatus.NormalExit
            and not self._error_reported
        ):
            self._pending_exit = self._reported_exit
        else:
            self._pending_exit = (
                -1 if self._error_reported or status == QProcess.ExitStatus.CrashExit else code
            )
        if not self._pid and not self._closing:
            self._fail("Windows terminal closed before it was ready.")
            self._pending_exit = -1
        self._schedule_output()

    def _complete(self, code: int) -> None:
        if self._completed:
            return
        self._completed = True
        self._pid = None
        self._input_pending = 0
        self._pending_exit = None
        self._helper_result = None
        for timer in (
            self._startup_timer,
            self._output_timer,
            self._control_timer,
            self._resize_timer,
            self._kill_timer,
        ):
            timer.stop()
        self.finished.emit(code)

    def _fail(self, message: str) -> None:
        if not self._error_reported:
            self._error_reported = True
            self.error_occurred.emit(message)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if not self._closing:
            self._fail("Could not start or communicate with the Windows terminal helper.")
        if error == QProcess.ProcessError.FailedToStart:
            self._complete(-1)
        elif error != QProcess.ProcessError.Crashed:
            self.close()

    def _startup_timeout(self) -> None:
        self._fail("Windows terminal startup timed out.")
        self.close()
