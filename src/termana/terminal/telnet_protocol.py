"""Bounded incremental Telnet negotiation and NVT data framing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, auto
from typing import Final

MAX_TELNET_INPUT_BYTES_PER_FEED: Final = 64 * 1024
MAX_TELNET_SUBNEGOTIATION_BYTES: Final = 4 * 1024
DEFAULT_TELNET_COLUMNS: Final = 80
DEFAULT_TELNET_ROWS: Final = 24
TERMINAL_TYPE: Final = b"xterm-256color"


class TelnetCommand(IntEnum):
    """Telnet command bytes used by the client protocol engine."""

    SE = 240
    NOP = 241
    DATA_MARK = 242
    BREAK = 243
    INTERRUPT_PROCESS = 244
    ABORT_OUTPUT = 245
    ARE_YOU_THERE = 246
    ERASE_CHARACTER = 247
    ERASE_LINE = 248
    GO_AHEAD = 249
    SB = 250
    WILL = 251
    WONT = 252
    DO = 253
    DONT = 254
    IAC = 255


class TelnetOption(IntEnum):
    """Options implemented by Termana's Telnet client."""

    BINARY = 0
    ECHO = 1
    SUPPRESS_GO_AHEAD = 3
    TERMINAL_TYPE = 24
    WINDOW_SIZE = 31


class TerminalTypeCommand(IntEnum):
    """Subnegotiation qualifiers for the TERMINAL-TYPE option."""

    IS = 0
    SEND = 1


class TelnetProtocolError(ValueError):
    """Report malformed or unbounded peer-controlled Telnet framing."""


@dataclass(frozen=True, slots=True)
class TelnetUpdate:
    """Application data and protocol replies produced by one input chunk."""

    application_data: bytes = b""
    outgoing_data: bytes = b""


class _ParserState(IntEnum):
    DATA = auto()
    IAC = auto()
    NEGOTIATION_OPTION = auto()
    SUBNEGOTIATION_OPTION = auto()
    SUBNEGOTIATION_DATA = auto()
    SUBNEGOTIATION_IAC = auto()


class _OptionState(IntEnum):
    """RFC 1143 state for one direction of one Telnet option."""

    NO = auto()
    WANTNO = auto()
    WANTYES = auto()
    YES = auto()


_LOCAL_SUPPORTED_OPTIONS: Final = frozenset(
    {
        TelnetOption.BINARY,
        TelnetOption.SUPPRESS_GO_AHEAD,
        TelnetOption.TERMINAL_TYPE,
        TelnetOption.WINDOW_SIZE,
    }
)
_REMOTE_SUPPORTED_OPTIONS: Final = frozenset(
    {
        TelnetOption.BINARY,
        TelnetOption.ECHO,
        TelnetOption.SUPPRESS_GO_AHEAD,
    }
)
_INITIAL_LOCAL_OPTIONS: Final = (
    TelnetOption.BINARY,
    TelnetOption.SUPPRESS_GO_AHEAD,
    TelnetOption.TERMINAL_TYPE,
    TelnetOption.WINDOW_SIZE,
)
_INITIAL_REMOTE_OPTIONS: Final = (
    TelnetOption.BINARY,
    TelnetOption.ECHO,
    TelnetOption.SUPPRESS_GO_AHEAD,
)


class TelnetProtocol:
    """Parse a Telnet byte stream without retaining unbounded peer input."""

    def __init__(
        self,
        *,
        columns: int = DEFAULT_TELNET_COLUMNS,
        rows: int = DEFAULT_TELNET_ROWS,
    ) -> None:
        """Create one cold protocol state with an initial terminal size."""
        self._validate_dimensions(columns, rows)
        self._columns = columns
        self._rows = rows
        self._state = _ParserState.DATA
        self._negotiation_command: TelnetCommand | None = None
        self._subnegotiation_option: int | None = None
        self._subnegotiation_data = bytearray()
        self._subnegotiation_length = 0
        self._incoming_pending_cr = False
        self._initial_negotiation_sent = False
        self._local_states: dict[int, _OptionState] = {}
        self._local_queued_opposites: set[int] = set()
        self._remote_states: dict[int, _OptionState] = {}
        self._remote_queued_opposites: set[int] = set()
        self._remote_echo_was_active = False

    @property
    def remote_echo_enabled(self) -> bool:
        """Return whether the peer currently owns terminal input echoing."""
        return self._option_enabled(self._remote_states, TelnetOption.ECHO)

    @property
    def remote_echo_was_active(self) -> bool:
        """Return whether remote echo was ever accepted during this session.

        This sticky fact lets the transport avoid exposing password input after
        a server deliberately turns a previously active echo option back off.
        """
        return self._remote_echo_was_active

    def initial_negotiation(self) -> bytes:
        """Advertise the supported client and server-side options once."""
        if self._initial_negotiation_sent:
            return b""
        self._initial_negotiation_sent = True
        outgoing = bytearray()
        for option in _INITIAL_LOCAL_OPTIONS:
            outgoing.extend(self._request_local_option(option, enabled=True))
        for option in _INITIAL_REMOTE_OPTIONS:
            outgoing.extend(self._request_remote_option(option, enabled=True))
        return bytes(outgoing)

    def feed(self, data: bytes) -> TelnetUpdate:
        """Consume one bounded fragment and return data plus immediate replies."""
        if not isinstance(data, bytes):
            raise TypeError("Telnet input must be bytes")
        if len(data) > MAX_TELNET_INPUT_BYTES_PER_FEED:
            raise TelnetProtocolError("Telnet input chunk exceeds the supported size limit")

        application_data = bytearray()
        outgoing_data = bytearray()
        for value in data:
            if self._state is _ParserState.DATA:
                if value == TelnetCommand.IAC:
                    self._state = _ParserState.IAC
                else:
                    self._append_application_byte(application_data, value)
                continue

            if self._state is _ParserState.IAC:
                if value == TelnetCommand.IAC:
                    self._append_application_byte(application_data, value)
                    self._state = _ParserState.DATA
                elif value in {
                    TelnetCommand.DO,
                    TelnetCommand.DONT,
                    TelnetCommand.WILL,
                    TelnetCommand.WONT,
                }:
                    self._negotiation_command = TelnetCommand(value)
                    self._state = _ParserState.NEGOTIATION_OPTION
                elif value == TelnetCommand.SB:
                    self._state = _ParserState.SUBNEGOTIATION_OPTION
                else:
                    self._state = _ParserState.DATA
                continue

            if self._state is _ParserState.NEGOTIATION_OPTION:
                command = self._negotiation_command
                self._negotiation_command = None
                self._state = _ParserState.DATA
                if command is not None:
                    outgoing_data.extend(self._handle_negotiation(command, value, application_data))
                continue

            if self._state is _ParserState.SUBNEGOTIATION_OPTION:
                self._subnegotiation_option = value
                self._subnegotiation_data.clear()
                self._subnegotiation_length = 0
                self._state = _ParserState.SUBNEGOTIATION_DATA
                continue

            if self._state is _ParserState.SUBNEGOTIATION_DATA:
                if value == TelnetCommand.IAC:
                    self._state = _ParserState.SUBNEGOTIATION_IAC
                else:
                    self._append_subnegotiation_byte(value)
                continue

            if self._state is _ParserState.SUBNEGOTIATION_IAC:
                if value == TelnetCommand.IAC:
                    self._append_subnegotiation_byte(value)
                    self._state = _ParserState.SUBNEGOTIATION_DATA
                elif value == TelnetCommand.SE:
                    outgoing_data.extend(self._finish_subnegotiation())
                    self._reset_subnegotiation()
                else:
                    self._reset_subnegotiation()
                    raise TelnetProtocolError("Malformed Telnet subnegotiation")

        return TelnetUpdate(bytes(application_data), bytes(outgoing_data))

    def encode_data(self, data: bytes) -> bytes:
        """Encode user bytes using active BINARY or NVT transmission rules."""
        if not isinstance(data, bytes):
            raise TypeError("Telnet output must be bytes")
        if self._option_enabled(self._local_states, TelnetOption.BINARY):
            encoded = data
        else:
            encoded_buffer = bytearray()
            index = 0
            while index < len(data):
                value = data[index]
                if value == 13:
                    if index + 1 < len(data) and data[index + 1] == 10:
                        encoded_buffer.extend(b"\r\n")
                        index += 2
                        continue
                    # TerminalInputEncoder publishes Return as a bare CR. On a
                    # network virtual terminal it is the user's Enter key.
                    encoded_buffer.extend(b"\r\n")
                elif value == 10:
                    encoded_buffer.extend(b"\r\n")
                else:
                    encoded_buffer.append(value)
                index += 1
            encoded = bytes(encoded_buffer)
        return self._escape_iac(encoded)

    def resize(self, columns: int, rows: int) -> bytes:
        """Remember the terminal size and publish NAWS when it is enabled."""
        self._validate_dimensions(columns, rows)
        if (columns, rows) == (self._columns, self._rows):
            return b""
        self._columns = columns
        self._rows = rows
        if not self._option_enabled(self._local_states, TelnetOption.WINDOW_SIZE):
            return b""
        return self._window_size_subnegotiation()

    def finish(self) -> bytes:
        """Flush a final pending NVT carriage return and discard framing tails."""
        application_data = b"\r" if self._incoming_pending_cr else b""
        self._incoming_pending_cr = False
        self._negotiation_command = None
        self._reset_subnegotiation()
        self._state = _ParserState.DATA
        return application_data

    def _handle_negotiation(
        self,
        command: TelnetCommand,
        option: int,
        application_data: bytearray,
    ) -> bytes:
        """Apply one RFC-style option transition without negotiation loops."""
        if command is TelnetCommand.DO:
            return self._handle_do(option)
        if command is TelnetCommand.DONT:
            return self._handle_dont(option)
        if command is TelnetCommand.WILL:
            outgoing, became_enabled = self._receive_positive(
                self._remote_states,
                self._remote_queued_opposites,
                option,
                agree=option in _REMOTE_SUPPORTED_OPTIONS,
                positive_response=TelnetCommand.DO,
                negative_response=TelnetCommand.DONT,
            )
            # A peer that announces WILL ECHO may display subsequent input
            # until it receives our reply, even when RFC 1143 immediately
            # transitions a crossed request to WANTNO. Keep this safety fact
            # separate from whether the option's final state is YES.
            if option == TelnetOption.ECHO:
                self._remote_echo_was_active = True
            if option == TelnetOption.BINARY and became_enabled:
                self._flush_incoming_cr(application_data)
            return outgoing
        if command is TelnetCommand.WONT:
            return self._receive_negative(
                self._remote_states,
                self._remote_queued_opposites,
                option,
                positive_response=TelnetCommand.DO,
                negative_response=TelnetCommand.DONT,
            )
        return b""

    def _handle_do(self, option: int) -> bytes:
        outgoing, became_enabled = self._receive_positive(
            self._local_states,
            self._local_queued_opposites,
            option,
            agree=option in _LOCAL_SUPPORTED_OPTIONS,
            positive_response=TelnetCommand.WILL,
            negative_response=TelnetCommand.WONT,
        )
        response = bytearray(outgoing)
        if option == TelnetOption.WINDOW_SIZE and became_enabled:
            response.extend(self._window_size_subnegotiation())
        return bytes(response)

    def _handle_dont(self, option: int) -> bytes:
        return self._receive_negative(
            self._local_states,
            self._local_queued_opposites,
            option,
            positive_response=TelnetCommand.WILL,
            negative_response=TelnetCommand.WONT,
        )

    def _request_local_option(self, option: int, *, enabled: bool) -> bytes:
        """Queue or send one request concerning the client side of an option."""
        return self._request_option(
            self._local_states,
            self._local_queued_opposites,
            option,
            enabled=enabled,
            positive_request=TelnetCommand.WILL,
            negative_request=TelnetCommand.WONT,
        )

    def _request_remote_option(self, option: int, *, enabled: bool) -> bytes:
        """Queue or send one request concerning the server side of an option."""
        return self._request_option(
            self._remote_states,
            self._remote_queued_opposites,
            option,
            enabled=enabled,
            positive_request=TelnetCommand.DO,
            negative_request=TelnetCommand.DONT,
        )

    @classmethod
    def _request_option(
        cls,
        states: dict[int, _OptionState],
        queued_opposites: set[int],
        option: int,
        *,
        enabled: bool,
        positive_request: TelnetCommand,
        negative_request: TelnetCommand,
    ) -> bytes:
        """Apply RFC 1143's user/process request table."""
        state = states.get(option, _OptionState.NO)
        queued_opposite = option in queued_opposites
        if enabled:
            if state is _OptionState.NO:
                cls._set_option_state(states, option, _OptionState.WANTYES)
                return cls._negotiation(positive_request, option)
            if state is _OptionState.WANTNO:
                if not queued_opposite:
                    queued_opposites.add(option)
            elif state is _OptionState.WANTYES and queued_opposite:
                queued_opposites.discard(option)
            return b""

        if state is _OptionState.YES:
            cls._set_option_state(states, option, _OptionState.WANTNO)
            return cls._negotiation(negative_request, option)
        if state is _OptionState.WANTNO and queued_opposite:
            queued_opposites.discard(option)
        elif state is _OptionState.WANTYES and not queued_opposite:
            queued_opposites.add(option)
        return b""

    @classmethod
    def _receive_positive(
        cls,
        states: dict[int, _OptionState],
        queued_opposites: set[int],
        option: int,
        *,
        agree: bool,
        positive_response: TelnetCommand,
        negative_response: TelnetCommand,
    ) -> tuple[bytes, bool]:
        """Apply RFC 1143's WILL/DO table and report a new YES state."""
        state = states.get(option, _OptionState.NO)
        queued_opposite = option in queued_opposites
        if state is _OptionState.NO:
            if not agree:
                return cls._negotiation(negative_response, option), False
            cls._set_option_state(states, option, _OptionState.YES)
            return cls._negotiation(positive_response, option), True

        if state is _OptionState.YES:
            return b"", False
        if state is _OptionState.WANTNO:
            if queued_opposite:
                queued_opposites.discard(option)
                cls._set_option_state(states, option, _OptionState.YES)
                return b"", True
            cls._set_option_state(states, option, _OptionState.NO)
            return b"", False
        if queued_opposite:
            queued_opposites.discard(option)
            cls._set_option_state(states, option, _OptionState.WANTNO)
            return cls._negotiation(negative_response, option), False
        cls._set_option_state(states, option, _OptionState.YES)
        return b"", True

    @classmethod
    def _receive_negative(
        cls,
        states: dict[int, _OptionState],
        queued_opposites: set[int],
        option: int,
        *,
        positive_response: TelnetCommand,
        negative_response: TelnetCommand,
    ) -> bytes:
        """Apply RFC 1143's WONT/DONT table."""
        state = states.get(option, _OptionState.NO)
        queued_opposite = option in queued_opposites
        if state is _OptionState.NO:
            return b""
        if state is _OptionState.YES:
            cls._set_option_state(states, option, _OptionState.NO)
            return cls._negotiation(negative_response, option)
        if state is _OptionState.WANTNO:
            if queued_opposite:
                queued_opposites.discard(option)
                cls._set_option_state(states, option, _OptionState.WANTYES)
                return cls._negotiation(positive_response, option)
            cls._set_option_state(states, option, _OptionState.NO)
            return b""
        queued_opposites.discard(option)
        cls._set_option_state(states, option, _OptionState.NO)
        return b""

    @staticmethod
    def _set_option_state(
        states: dict[int, _OptionState],
        option: int,
        state: _OptionState,
    ) -> None:
        """Keep implicit NO states out of the bounded option-state maps."""
        if state is _OptionState.NO:
            states.pop(option, None)
        else:
            states[option] = state

    @staticmethod
    def _option_enabled(states: dict[int, _OptionState], option: int) -> bool:
        return states.get(option, _OptionState.NO) is _OptionState.YES

    def _finish_subnegotiation(self) -> bytes:
        option = self._subnegotiation_option
        payload = bytes(self._subnegotiation_data)
        if (
            option == TelnetOption.TERMINAL_TYPE
            and self._option_enabled(self._local_states, TelnetOption.TERMINAL_TYPE)
            and payload == bytes((TerminalTypeCommand.SEND,))
        ):
            return self._subnegotiation(
                TelnetOption.TERMINAL_TYPE,
                bytes((TerminalTypeCommand.IS,)) + TERMINAL_TYPE,
            )
        return b""

    def _append_subnegotiation_byte(self, value: int) -> None:
        self._subnegotiation_length += 1
        if self._subnegotiation_length > MAX_TELNET_SUBNEGOTIATION_BYTES:
            self._reset_subnegotiation()
            raise TelnetProtocolError("Telnet subnegotiation exceeds the supported size limit")
        if self._subnegotiation_option == TelnetOption.TERMINAL_TYPE:
            self._subnegotiation_data.append(value)

    def _append_application_byte(self, output: bytearray, value: int) -> None:
        if self._option_enabled(self._remote_states, TelnetOption.BINARY):
            output.append(value)
            return
        if self._incoming_pending_cr:
            self._incoming_pending_cr = False
            if value == 0:
                output.append(13)
                return
            if value == 10:
                output.extend(b"\r\n")
                return
            output.append(13)
        if value == 13:
            self._incoming_pending_cr = True
        else:
            output.append(value)

    def _flush_incoming_cr(self, output: bytearray) -> None:
        if self._incoming_pending_cr:
            output.append(13)
            self._incoming_pending_cr = False

    def _window_size_subnegotiation(self) -> bytes:
        payload = self._columns.to_bytes(2, "big") + self._rows.to_bytes(2, "big")
        return self._subnegotiation(TelnetOption.WINDOW_SIZE, payload)

    def _reset_subnegotiation(self) -> None:
        self._subnegotiation_option = None
        self._subnegotiation_data.clear()
        self._subnegotiation_length = 0
        self._state = _ParserState.DATA

    @staticmethod
    def _negotiation(command: TelnetCommand, option: int) -> bytes:
        return bytes((TelnetCommand.IAC, command, option))

    @classmethod
    def _subnegotiation(cls, option: int, payload: bytes) -> bytes:
        return (
            bytes((TelnetCommand.IAC, TelnetCommand.SB, option))
            + cls._escape_iac(payload)
            + bytes((TelnetCommand.IAC, TelnetCommand.SE))
        )

    @staticmethod
    def _escape_iac(data: bytes) -> bytes:
        return data.replace(
            bytes((TelnetCommand.IAC,)),
            bytes((TelnetCommand.IAC, TelnetCommand.IAC)),
        )

    @staticmethod
    def _validate_dimensions(columns: int, rows: int) -> None:
        if (
            isinstance(columns, bool)
            or not isinstance(columns, int)
            or isinstance(rows, bool)
            or not isinstance(rows, int)
            or not 1 <= columns <= 65535
            or not 1 <= rows <= 65535
        ):
            raise ValueError("Telnet terminal dimensions must be between 1 and 65535")
