"""Asynchronous built-in Telnet transport backed by ``QTcpSocket``."""

from __future__ import annotations

import codecs
from collections import deque
from collections.abc import Callable
from typing import Final

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket
from wcwidth import wcwidth

from termana.models.connection import TelnetEncoding
from termana.terminal.backend import (
    MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN,
    TerminalBackend,
)
from termana.terminal.telnet_protocol import (
    TelnetProtocol,
    TelnetProtocolError,
)

MAX_TELNET_SOCKET_READ_BUFFER_BYTES: Final = 1024 * 1024
MAX_PENDING_TELNET_WRITE_BYTES: Final = 1024 * 1024
MAX_TELNET_INPUT_BYTES_PER_WRITE: Final = MAX_PENDING_TELNET_WRITE_BYTES // 2
MAX_TELNET_LOCAL_ECHO_BYTES_PER_WRITE: Final = MAX_TELNET_INPUT_BYTES_PER_WRITE * 3
MAX_TELNET_LOCAL_ECHO_TRACKED_GLYPHS: Final = 4 * 1024
TELNET_LOCAL_ECHO_TAB_STOP: Final = 8
TELNET_CONNECT_TIMEOUT_MS: Final = 10_000
TELNET_DISCONNECT_TIMEOUT_MS: Final = 500

type TelnetBackendFactory = Callable[[str, int, TelnetEncoding], TerminalBackend]


def create_telnet_backend(
    host: str,
    port: int,
    encoding: TelnetEncoding = TelnetEncoding.UTF8,
) -> TerminalBackend:
    """Create the default built-in Telnet transport for one endpoint."""
    return TelnetTerminalBackend(host, port, encoding=encoding)


class TelnetTerminalBackend(TerminalBackend):
    """Expose one non-blocking Telnet connection through the terminal API."""

    @property
    def uses_legacy_console_colors(self) -> bool:
        return True

    def __init__(
        self,
        host: str,
        port: int,
        parent=None,
        *,
        encoding: TelnetEncoding = TelnetEncoding.UTF8,
    ) -> None:
        """Create a cold client for one validated network endpoint."""
        super().__init__(parent)
        if not isinstance(host, str) or not host or host != host.strip():
            raise ValueError("Telnet host must be a non-empty trimmed string")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Telnet port must be between 1 and 65535")
        if not isinstance(encoding, TelnetEncoding):
            raise ValueError("Telnet encoding must be a supported TelnetEncoding")

        self._host = host
        self._port = port
        self._encoding = encoding
        self._display_endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self._protocol = TelnetProtocol()
        self._start_attempted = False
        self._completion_emitted = True
        self._connected = False
        self._closing = False
        self._failed = False
        self._pending_disconnect_code: int | None = None
        self._local_echo_decoder: codecs.IncrementalDecoder = codecs.getincrementaldecoder(
            self.text_encoding
        )(errors="ignore")
        self._local_echo_glyphs: deque[tuple[int, int]] = deque(
            maxlen=MAX_TELNET_LOCAL_ECHO_TRACKED_GLYPHS
        )
        self._prepared_input_context: tuple[int, int] | None = None

        self._socket = QTcpSocket(self)
        self._socket.setReadBufferSize(MAX_TELNET_SOCKET_READ_BUFFER_BYTES)
        self._socket.connected.connect(self._handle_connected)
        self._socket.readyRead.connect(self._schedule_read)
        self._socket.disconnected.connect(self._handle_disconnected)
        self._socket.errorOccurred.connect(self._handle_socket_error)

        self._read_timer = QTimer(self)
        self._read_timer.setSingleShot(True)
        self._read_timer.timeout.connect(self._read_one_chunk)

        self._disconnect_timer = QTimer(self)
        self._disconnect_timer.setSingleShot(True)
        self._disconnect_timer.timeout.connect(self._force_disconnect)

        self._connect_timer = QTimer(self)
        self._connect_timer.setSingleShot(True)
        self._connect_timer.timeout.connect(self._handle_connect_timeout)

    @property
    def is_running(self) -> bool:
        """Return whether a connection attempt or established socket is active."""
        return self._socket.state() != QAbstractSocket.SocketState.UnconnectedState

    @property
    def text_encoding(self) -> str:
        """Return the selected codec for terminal input, output, and local echo."""
        return self._encoding.value

    @property
    def echoes_input(self) -> bool:
        """Treat Telnet as an interactive terminal whose peer controls echoing."""
        return True

    @property
    def supports_direct_input(self) -> bool:
        """Telnet accepts raw terminal keystrokes rather than whole commands."""
        return True

    def start(self) -> None:
        """Begin one asynchronous TCP connection attempt."""
        if self._start_attempted:
            return
        self._start_attempted = True
        self._completion_emitted = False
        self._closing = False
        self._failed = False
        self._connect_timer.start(TELNET_CONNECT_TIMEOUT_MS)
        self._socket.connectToHost(self._host, self._port)

    def write(self, data: bytes) -> None:
        """Encode and queue terminal data only while the socket is connected."""
        input_context = self._prepared_input_context
        self._prepared_input_context = None
        if not isinstance(data, bytes):
            raise TypeError("Telnet output must be bytes")
        if (
            not self._connected
            or self._closing
            or self._completion_emitted
            or self._socket.state() is not QAbstractSocket.SocketState.ConnectedState
        ):
            self.warning_occurred.emit("The Telnet connection is not ready for input.")
            return
        if len(data) > MAX_TELNET_INPUT_BYTES_PER_WRITE:
            self.warning_occurred.emit("The Telnet input exceeds the supported size limit.")
            return
        payload = self._protocol.encode_data(data)
        if not self._write_wire_data(payload):
            return
        if self._local_echo_is_safe and input_context is not None:
            cursor_column, columns = input_context
            if local_echo := self._encode_local_echo(
                data,
                cursor_column=cursor_column,
                columns=columns,
            ):
                self.data_received.emit(local_echo)
        elif not self._local_echo_is_safe:
            self._reset_local_echo_state()

    def prepare_input(self, cursor_column: int, columns: int) -> None:
        """Snapshot the terminal cursor used to render safe local feedback."""
        if (
            isinstance(cursor_column, bool)
            or not isinstance(cursor_column, int)
            or isinstance(columns, bool)
            or not isinstance(columns, int)
            or columns < 1
            or not 0 <= cursor_column <= columns
        ):
            raise ValueError("Telnet input cursor context is outside the terminal grid")
        self._prepared_input_context = (cursor_column, columns)

    def resize(self, columns: int, rows: int) -> None:
        """Remember dimensions and send NAWS after the peer enables it."""
        self._reset_local_echo_state()
        payload = self._protocol.resize(columns, rows)
        if payload and self._connected and not self._closing:
            self._write_wire_data(payload)

    def close(self) -> None:
        """Begin a non-blocking disconnect and complete once at most."""
        if self._completion_emitted or self._closing:
            return
        self._closing = True
        state = self._socket.state()
        if state is QAbstractSocket.SocketState.UnconnectedState:
            if (
                self._socket.bytesAvailable() > 0
                or self._read_timer.isActive()
                or self._pending_disconnect_code is not None
            ):
                if self._pending_disconnect_code is None:
                    self._pending_disconnect_code = -1 if self._failed else 0
                self._schedule_read()
                return
            self._complete(0)
            return
        self._read_timer.stop()
        if state is not QAbstractSocket.SocketState.ConnectedState:
            self._socket.abort()
            self._complete(0)
            return
        self._socket.disconnectFromHost()
        if self._socket.state() is QAbstractSocket.SocketState.UnconnectedState:
            self._complete(0)
        else:
            self._disconnect_timer.start(TELNET_DISCONNECT_TIMEOUT_MS)

    def _handle_connected(self) -> None:
        """Advertise capabilities and publish readiness exactly once."""
        if self._completion_emitted:
            self._socket.abort()
            return
        self._connect_timer.stop()
        if self._closing:
            self._socket.disconnectFromHost()
            return
        self._connected = True
        self._write_wire_data(self._protocol.initial_negotiation())
        self.started.emit()

    def _schedule_read(self) -> None:
        """Defer one bounded read so sustained traffic yields to Qt."""
        if not self._completion_emitted and not self._read_timer.isActive():
            self._read_timer.start(0)

    def _read_one_chunk(self) -> None:
        """Parse at most one terminal-sized network slice per event-loop turn."""
        if self._completion_emitted:
            return
        raw_data = bytes(self._socket.read(MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN))
        if raw_data:
            try:
                update = self._protocol.feed(raw_data)
            except TelnetProtocolError as error:
                self._fail(f"Telnet protocol error: {error}")
                return
            if not self._local_echo_is_safe:
                self._reset_local_echo_state()
            if update.application_data:
                self._reset_local_echo_state()
                self.data_received.emit(update.application_data)
            if update.outgoing_data:
                self._write_wire_data(update.outgoing_data)

        if self._socket.bytesAvailable() > 0:
            self._read_timer.start(0)
        elif self._pending_disconnect_code is not None:
            exit_code = self._pending_disconnect_code
            self._pending_disconnect_code = None
            self._complete(exit_code)

    def _handle_disconnected(self) -> None:
        """Drain bytes already delivered by Qt before publishing completion."""
        self._connected = False
        self._disconnect_timer.stop()
        if self._completion_emitted:
            return
        exit_code = -1 if self._failed else 0
        if self._socket.bytesAvailable() > 0:
            self._pending_disconnect_code = exit_code
            self._schedule_read()
            return
        self._complete(exit_code)

    def _handle_socket_error(self, error: QAbstractSocket.SocketError) -> None:
        """Distinguish an orderly remote close from terminal network errors."""
        if self._completion_emitted or self._closing:
            return
        if error is QAbstractSocket.SocketError.RemoteHostClosedError and self._connected:
            return
        self._fail(
            f"Could not connect Telnet to {self._display_endpoint}: {self._socket.errorString()}"
        )

    def _fail(self, message: str) -> None:
        """Abort one failed connection after a single user-visible error."""
        if self._completion_emitted:
            return
        self._failed = True
        # Signal handlers may synchronously ask the backend to close. Mark this
        # lifecycle as closing first so they cannot replace the failure code.
        self._closing = True
        self.error_occurred.emit(message)
        self._socket.abort()
        self._complete(-1)

    def _handle_connect_timeout(self) -> None:
        """Fail a stalled DNS or TCP attempt without blocking the application."""
        if self._completion_emitted or self._connected or self._closing:
            return
        self._fail(f"Could not connect Telnet to {self._display_endpoint}: timed out")

    def _write_wire_data(self, payload: bytes) -> bool:
        """Bound Qt's pending write queue for user data and negotiations alike."""
        if not payload or self._completion_emitted:
            return False
        if self._socket.state() is not QAbstractSocket.SocketState.ConnectedState:
            return False
        if self._socket.bytesToWrite() + len(payload) > MAX_PENDING_TELNET_WRITE_BYTES:
            self.warning_occurred.emit("The Telnet output buffer is full.")
            return False
        bytes_queued = self._socket.write(payload)
        if bytes_queued < 0:
            self.warning_occurred.emit("Could not write to the Telnet connection.")
            return False
        return bytes_queued == len(payload)

    @property
    def _local_echo_is_safe(self) -> bool:
        """Echo only before this peer has ever taken control of input echoing."""
        return not self._protocol.remote_echo_enabled and not self._protocol.remote_echo_was_active

    def _encode_local_echo(
        self,
        data: bytes,
        *,
        cursor_column: int,
        columns: int,
    ) -> bytes:
        """Return bounded, display-safe local feedback for terminal input.

        Escape-prefixed terminal keys and arbitrary controls are deliberately
        excluded. The incremental decoder preserves characters split across
        writes according to the selected session encoding.
        """
        if self._local_echo_glyphs:
            start_column, occupied_width = self._local_echo_glyphs[-1]
            if cursor_column != start_column + occupied_width:
                self._local_echo_glyphs.clear()

        escape_index = data.find(b"\x1b")
        visible_data = data if escape_index < 0 else data[:escape_index]
        text = self._local_echo_decoder.decode(visible_data, final=False)

        output = bytearray()
        index = 0
        while index < len(text):
            character = text[index]
            if character == "\r":
                encoded = b"\r\n"
                self._local_echo_glyphs.clear()
                cursor_column = 0
                if index + 1 < len(text) and text[index + 1] == "\n":
                    index += 1
            elif character == "\n":
                encoded = b"\r\n"
                self._local_echo_glyphs.clear()
                cursor_column = 0
            elif character in {"\b", "\x7f"}:
                if self._local_echo_glyphs:
                    start_column, occupied_width = self._local_echo_glyphs.pop()
                    encoded = self._local_erase_sequence(start_column, occupied_width)
                    cursor_column = start_column
                else:
                    encoded = b""
            elif character == "\t":
                encoded = b"\t"
                self._local_echo_glyphs.clear()
                cursor_column = next(
                    (
                        stop
                        for stop in range(
                            TELNET_LOCAL_ECHO_TAB_STOP,
                            columns,
                            TELNET_LOCAL_ECHO_TAB_STOP,
                        )
                        if stop > cursor_column
                    ),
                    columns - 1,
                )
            elif character.isprintable():
                width = wcwidth(character)
                if width > 0:
                    encoded = character.encode(self.text_encoding)
                    if cursor_column >= columns:
                        self._local_echo_glyphs.clear()
                        cursor_column = 0
                    remaining_columns = columns - cursor_column
                    occupied_width = min(width, remaining_columns)
                    if occupied_width > 0:
                        self._local_echo_glyphs.append((cursor_column, occupied_width))
                        cursor_column += occupied_width
                elif width == 0 and self._local_echo_glyphs:
                    encoded = character.encode(self.text_encoding)
                else:
                    encoded = b""
            else:
                encoded = b""
                self._local_echo_glyphs.clear()

            remaining = MAX_TELNET_LOCAL_ECHO_BYTES_PER_WRITE - len(output)
            if len(encoded) > remaining:
                self._reset_local_echo_state()
                break
            output.extend(encoded)
            index += 1
        if escape_index >= 0:
            self._reset_local_echo_state()
        return bytes(output)

    @staticmethod
    def _local_erase_sequence(start_column: int, occupied_width: int) -> bytes:
        """Erase owned cells with absolute positioning safe at pending-wrap."""
        move_to_start = f"\x1b[{start_column + 1}G".encode("ascii")
        return move_to_start + b" " * occupied_width + move_to_start

    def _reset_local_echo_state(self) -> None:
        """Forget partial text whenever local cursor ownership becomes uncertain."""
        self._local_echo_decoder.reset()
        self._local_echo_glyphs.clear()
        self._prepared_input_context = None

    def _force_disconnect(self) -> None:
        """Bound shutdown time without blocking the GUI thread."""
        if self._completion_emitted:
            return
        self._socket.abort()
        self._complete(0)

    def _complete(self, exit_code: int) -> None:
        """Publish all final data and completion exactly once."""
        if self._completion_emitted:
            return
        self._completion_emitted = True
        self._connected = False
        self._read_timer.stop()
        self._connect_timer.stop()
        self._disconnect_timer.stop()
        self._reset_local_echo_state()
        final_data = self._protocol.finish()
        if final_data:
            self.data_received.emit(final_data)
        self.finished.emit(exit_code)
