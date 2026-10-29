"""Minimal, fail-closed SSH_ASKPASS client for Termana's local broker."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import socket
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO, Final, Protocol

ASKPASS_MODE_ENV: Final = "TERMANA_ASKPASS_MODE"
ASKPASS_PROTOCOL_ENV: Final = "TERMANA_ASKPASS_PROTOCOL"
ASKPASS_SOCKET_ENV: Final = "TERMANA_ASKPASS_SOCKET"
ASKPASS_TOKEN_ENV: Final = "TERMANA_ASKPASS_TOKEN"
ASKPASS_EXPECTED_PARENT_PID_ENV: Final = "TERMANA_ASKPASS_EXPECTED_PARENT_PID"
SSH_ASKPASS_PROMPT_ENV: Final = "SSH_ASKPASS_PROMPT"

ASKPASS_MODE_VALUE: Final = "1"
ASKPASS_PROTOCOL_VERSION: Final = 1
ASKPASS_PROTOCOL_VALUE: Final = str(ASKPASS_PROTOCOL_VERSION)

CONNECT_TIMEOUT_SECONDS: Final = 3.0
RESPONSE_TIMEOUT_SECONDS: Final = 300.0
MAX_SOCKET_PATH_BYTES: Final = 103
TOKEN_BYTES: Final = 32
TOKEN_CHARACTERS: Final = 43
MAX_PROMPT_BYTES: Final = 4 * 1024
MAX_PROMPT_KIND_BYTES: Final = 64
MAX_REQUEST_BYTES: Final = 8 * 1024
MAX_RESPONSE_BYTES: Final = 8 * 1024
MAX_PASSWORD_BYTES: Final = 1023
MAX_PROCESS_ID: Final = (1 << 31) - 1

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1


class _AskpassFailure(Exception):
    """Internal fixed-message failure that is never rendered by the helper."""


class _UnixSocket(Protocol):
    """Small injectable socket surface used by the helper."""

    def settimeout(self, value: float | None) -> None: ...

    def connect(self, address: str) -> None: ...

    def sendall(self, data: bytes) -> None: ...

    def recv(self, bufsize: int) -> bytes: ...

    def close(self) -> None: ...


SocketFactory = Callable[[int, int], _UnixSocket]


class _FileDescriptorOutput:
    """Write directly to OpenSSH's validated output pipe."""

    def __init__(self, file_descriptor: int = 1) -> None:
        self._file_descriptor = file_descriptor

    def isatty(self) -> bool:
        descriptor_status = os.fstat(self._file_descriptor)
        if not stat.S_ISFIFO(descriptor_status.st_mode):
            raise _AskpassFailure("AskPass output must be OpenSSH's pipe")
        return os.isatty(self._file_descriptor)

    def write(self, data: bytes | bytearray) -> int:
        payload = memoryview(data)
        total = 0
        while total < len(payload):
            written = os.write(self._file_descriptor, payload[total:])
            if written <= 0:
                raise OSError("AskPass output closed")
            total += written
        return total

    def flush(self) -> None:
        return None


def is_askpass_invocation(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the process was explicitly launched in internal askpass mode.

    Detection uses the dedicated mode marker plus the parent binding that the PTY
    bootstrap installs immediately before it execs OpenSSH. This distinguishes the
    bootstrap process (which inherits the other broker fields) from a real AskPass
    child. Once selected, missing or malformed broker fields fail closed here.
    """
    environment = os.environ if environ is None else environ
    value = environment.get(ASKPASS_MODE_ENV)
    expected_parent = environment.get(ASKPASS_EXPECTED_PARENT_PID_ENV)
    return (
        isinstance(value, str)
        and value.isascii()
        and hmac.compare_digest(value, ASKPASS_MODE_VALUE)
        and isinstance(expected_parent, str)
        and bool(expected_parent)
    )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    socket_factory: SocketFactory = socket.socket,
    stdout: BinaryIO | None = None,
) -> int:
    """Request one password from the local broker without rendering failures.

    The only successful output is the broker-provided password followed by one
    newline on stdout. Every validation, IPC, cancellation, and output failure is a
    silent non-zero exit so neither exception details nor response data can leak.
    """
    try:
        arguments = sys.argv if argv is None else argv
        environment = os.environ if environ is None else environ
        output = _FileDescriptorOutput() if stdout is None else stdout
        return _run(arguments, environment, socket_factory=socket_factory, stdout=output)
    except BaseException:
        return EXIT_FAILURE


def _run(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    socket_factory: SocketFactory,
    stdout: BinaryIO,
) -> int:
    if not is_askpass_invocation(environ):
        raise _AskpassFailure("Askpass mode is not enabled")
    if _stdout_is_tty(stdout):
        raise _AskpassFailure("Askpass output must be a pipe")
    if len(argv) != 2 or not all(isinstance(argument, str) for argument in argv):
        raise _AskpassFailure("Invalid askpass arguments")

    protocol = _required_environment_value(environ, ASKPASS_PROTOCOL_ENV)
    if not hmac.compare_digest(protocol, ASKPASS_PROTOCOL_VALUE):
        raise _AskpassFailure("Unsupported askpass protocol")
    socket_path = _validated_socket_path(_required_environment_value(environ, ASKPASS_SOCKET_ENV))
    token = _validated_token(_required_environment_value(environ, ASKPASS_TOKEN_ENV))
    expected_parent_pid = _validated_environment_pid(
        _required_environment_value(environ, ASKPASS_EXPECTED_PARENT_PID_ENV)
    )
    helper_pid = _validated_process_pid(os.getpid())
    parent_pid = _validated_process_pid(os.getppid())
    prompt = _validated_bounded_text(argv[1], MAX_PROMPT_BYTES, "prompt")
    prompt_kind = _validated_prompt_kind(environ.get(SSH_ASKPASS_PROMPT_ENV, ""))
    request = _encode_request(
        token,
        prompt,
        prompt_kind,
        helper_pid=helper_pid,
        parent_pid=parent_pid,
        expected_parent_pid=expected_parent_pid,
    )

    response = _exchange(socket_path, request, socket_factory)
    password = _decode_response(response)
    if password is None:
        return EXIT_FAILURE

    payload = bytearray(password)
    payload.append(0x0A)
    try:
        written = stdout.write(payload)
        if written is not None and written != len(payload):
            raise _AskpassFailure("Incomplete askpass output")
        stdout.flush()
    finally:
        payload[:] = b"\x00" * len(payload)
    return EXIT_OK


def _required_environment_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        raise _AskpassFailure("Missing askpass configuration")
    return value


def _validated_socket_path(value: str) -> str:
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ) or not os.path.isabs(value):
        raise _AskpassFailure("Invalid askpass socket path")
    try:
        encoded = os.fsencode(value)
    except Exception:
        raise _AskpassFailure("Invalid askpass socket path") from None
    if not encoded or len(encoded) > MAX_SOCKET_PATH_BYTES:
        raise _AskpassFailure("Invalid askpass socket path")
    return value


def _validated_token(value: str) -> str:
    if len(value) != TOKEN_CHARACTERS or not value.isascii():
        raise _AskpassFailure("Invalid askpass capability")
    try:
        decoded = base64.b64decode(
            value + "=",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        raise _AskpassFailure("Invalid askpass capability") from None
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != TOKEN_BYTES or not hmac.compare_digest(canonical, value):
        raise _AskpassFailure("Invalid askpass capability")
    return value


def _validated_environment_pid(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise _AskpassFailure("Invalid askpass process binding")
    try:
        process_id = int(value, 10)
    except ValueError:
        raise _AskpassFailure("Invalid askpass process binding") from None
    if str(process_id) != value:
        raise _AskpassFailure("Invalid askpass process binding")
    return _validated_process_pid(process_id)


def _validated_process_pid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _AskpassFailure("Invalid askpass process binding")
    if value <= 0 or value > MAX_PROCESS_ID:
        raise _AskpassFailure("Invalid askpass process binding")
    return value


def _validated_bounded_text(value: str, maximum_bytes: int, field: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _AskpassFailure(f"Invalid askpass {field}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise _AskpassFailure(f"Invalid askpass {field}") from None
    if len(encoded) > maximum_bytes:
        raise _AskpassFailure(f"Invalid askpass {field}")
    return value


def _validated_prompt_kind(value: object) -> str:
    if not isinstance(value, str):
        raise _AskpassFailure("Invalid askpass prompt kind")
    value = _validated_bounded_text(value, MAX_PROMPT_KIND_BYTES, "prompt kind")
    if value not in {"", "confirm", "none"}:
        raise _AskpassFailure("Invalid askpass prompt kind")
    return value


def _encode_request(
    token: str,
    prompt: str,
    prompt_kind: str,
    *,
    helper_pid: int,
    parent_pid: int,
    expected_parent_pid: int,
) -> bytes:
    # Both PIDs are advisory protocol data. The broker must authenticate the peer
    # with kernel-provided credentials; neither JSON nor the token is that boundary.
    try:
        request = json.dumps(
            {
                "version": ASKPASS_PROTOCOL_VERSION,
                "token": token,
                "prompt": prompt,
                "prompt_kind": prompt_kind,
                "helper_pid": helper_pid,
                "parent_pid": parent_pid,
                "expected_parent_pid": expected_parent_pid,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        raise _AskpassFailure("Invalid askpass request") from None
    if len(request) > MAX_REQUEST_BYTES:
        raise _AskpassFailure("Askpass request exceeds the limit")
    return request + b"\n"


def _exchange(
    socket_path: str,
    request: bytes,
    socket_factory: SocketFactory,
) -> bytes:
    client: _UnixSocket | None = None
    try:
        client = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(CONNECT_TIMEOUT_SECONDS)
        client.connect(socket_path)
        client.sendall(request)
        client.settimeout(RESPONSE_TIMEOUT_SECONDS)
        return _receive_line(client)
    except _AskpassFailure:
        raise
    except BaseException:
        raise _AskpassFailure("Askpass broker is unavailable") from None
    finally:
        if client is not None:
            try:
                client.close()
            except BaseException:
                pass


def _receive_line(client: _UnixSocket) -> bytes:
    response = bytearray()
    while True:
        remaining = MAX_RESPONSE_BYTES + 1 - len(response)
        if remaining <= 0:
            raise _AskpassFailure("Askpass response exceeds the limit")
        chunk = client.recv(min(4096, remaining))
        if not chunk:
            raise _AskpassFailure("Incomplete askpass response")
        newline = chunk.find(b"\n")
        if newline >= 0:
            response.extend(chunk[:newline])
            if newline != len(chunk) - 1:
                raise _AskpassFailure("Invalid trailing askpass response data")
            if len(response) > MAX_RESPONSE_BYTES:
                raise _AskpassFailure("Askpass response exceeds the limit")
            return bytes(response)
        response.extend(chunk)


def _decode_response(response: bytes) -> bytes | None:
    try:
        text = response.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_strict_object)
    except BaseException:
        raise _AskpassFailure("Invalid askpass response") from None
    if not isinstance(payload, dict):
        raise _AskpassFailure("Invalid askpass response")

    status = payload.get("status")
    if status == "cancel":
        if set(payload) not in ({"status"}, {"status", "value"}):
            raise _AskpassFailure("Invalid askpass response")
        if "value" in payload and payload["value"] is not None:
            raise _AskpassFailure("Invalid askpass response")
        return None
    if status != "ok" or set(payload) != {"status", "value"}:
        raise _AskpassFailure("Invalid askpass response")

    value = payload["value"]
    if not isinstance(value, str) or any(character in value for character in "\x00\r\n"):
        raise _AskpassFailure("Invalid askpass response")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise _AskpassFailure("Invalid askpass response") from None
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise _AskpassFailure("Invalid askpass response")
    return encoded


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _AskpassFailure("Invalid askpass response")
        result[key] = value
    return result


def _stdout_is_tty(stdout: BinaryIO) -> bool:
    try:
        return bool(stdout.isatty())
    except (AttributeError, OSError):
        return True


if __name__ == "__main__":
    raise SystemExit(main())
