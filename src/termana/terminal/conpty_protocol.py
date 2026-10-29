"""Private framed input for the Windows console helper; never a shell command line."""

import json
import struct
import sys
from collections.abc import Callable

from termana.terminal.command import ShellCommand

MAX_FRAME_BYTES = 1024 * 1024
MAX_LAUNCH_BYTES = 128 * 1024
MAX_ARGUMENTS = 1024
MAX_DIMENSION = 32767  # ConPTY COORD uses signed 16-bit values.
IO_CHUNK_BYTES = 4096
HEADER = struct.Struct("!cI")
SIZE = struct.Struct("!HH")


def validate_size(columns: int, rows: int) -> None:
    if any(type(value) is not int or not 1 <= value <= MAX_DIMENSION for value in (columns, rows)):
        raise ValueError(f"Terminal dimensions must be integers between 1 and {MAX_DIMENSION}.")


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    if kind not in (b"L", b"I", b"R", b"A") or len(payload) > MAX_FRAME_BYTES:
        raise ValueError("Invalid console input frame")
    return HEADER.pack(kind, len(payload)) + payload


def launch_frame(command: ShellCommand, cwd: str, columns: int, rows: int) -> bytes:
    validate_size(columns, rows)
    payload = json.dumps(
        {
            "argv": [command.program, *command.arguments],
            "cwd": cwd,
            "columns": columns,
            "rows": rows,
        },
        ensure_ascii=True,
    ).encode("ascii")
    parse_launch(payload)
    return frame(b"L", payload)


def parse_launch(payload: bytes) -> tuple[list[str], str, int, int]:
    if len(payload) > MAX_LAUNCH_BYTES:
        raise ValueError("Console launch request is too large")
    values = json.loads(payload)
    if not isinstance(values, dict) or set(values) != {"argv", "cwd", "columns", "rows"}:
        raise ValueError("Invalid console launch request")
    argv, cwd = values["argv"], values["cwd"]
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > MAX_ARGUMENTS
        or any(not isinstance(part, str) or "\0" in part for part in argv)
        or not argv[0]
        or not isinstance(cwd, str)
        or not cwd
        or "\0" in cwd
    ):
        raise ValueError("Invalid console command or working directory")
    validate_size(values["columns"], values["rows"])
    return argv, cwd, values["columns"], values["rows"]


def read_exact(read: Callable[[int], bytes], count: int, *, allow_eof: bool = False) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = read(count - len(result))
        if not chunk:
            if allow_eof and not result:
                return b""
            raise ValueError("Truncated console input frame")
        result.extend(chunk)
    return bytes(result)


def read_frame(read: Callable[[int], bytes]) -> tuple[bytes, bytes] | None:
    header = read_exact(read, HEADER.size, allow_eof=True)
    if not header:
        return None
    kind, size = HEADER.unpack(header)
    if kind not in (b"L", b"I", b"R", b"A") or size > MAX_FRAME_BYTES:
        raise ValueError("Invalid console input frame")
    if (
        (kind == b"L" and size > MAX_LAUNCH_BYTES)
        or (kind == b"R" and size != SIZE.size)
        or (kind == b"A" and size != 0)
    ):
        raise ValueError("Invalid console control frame")
    return kind, read_exact(read, size)


def helper_invocation() -> ShellCommand:
    """Run the helper with the active console Python and inherited private pipes."""
    from termana.helper_runtime import helper_command

    return helper_command(
        "conpty", ShellCommand(sys.executable, ("-u", "-m", "termana.terminal.conpty_child"))
    )
