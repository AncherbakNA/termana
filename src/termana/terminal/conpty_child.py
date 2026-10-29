"""Disposable Windows-only native console host, isolated from the GUI event loop."""

import os
import sys
import threading

from termana.terminal.conpty_protocol import (
    HEADER,
    IO_CHUNK_BYTES,
    MAX_FRAME_BYTES,
    SIZE,
    parse_launch,
    read_frame,
    validate_size,
)

PUMP_WAIT_SECONDS = 0.05
INPUT_JOIN_TIMEOUT_SECONDS = 0.5
OUTPUT_DRAIN_TIMEOUT_SECONDS = 3


def _write_all(fd: int, data: bytes) -> None:
    while data:
        count = os.write(fd, data)
        if count <= 0:
            raise OSError("Closed helper output")
        data = data[count:]


def run_console(console, read, write_output, report) -> int:
    """Keep input, output and lifetime on separate paths. Testable without Windows."""
    stopping = threading.Event()
    parent_closed = threading.Event()
    credit = threading.Event()
    credit.set()
    input_condition = threading.Condition()
    pending_input = bytearray()
    pending_size: list[tuple[int, int]] = []

    def input_loop():
        try:
            while True:
                packet = read_frame(read)
                if packet is None:
                    parent_closed.set()
                    stopping.set()
                    return
                kind, data = packet
                if kind == b"A":
                    credit.set()
                    continue
                if stopping.is_set():
                    continue
                if kind == b"I":
                    with input_condition:
                        if len(pending_input) + len(data) > MAX_FRAME_BYTES:
                            raise ValueError("Console input queue overflow")
                        pending_input.extend(data)
                        input_condition.notify()
                elif kind == b"R":
                    size = SIZE.unpack(data)
                    validate_size(*size)
                    with input_condition:
                        pending_size[:] = [size]
                else:
                    raise ValueError("Repeated console launch")
        except (OSError, ValueError):
            if not stopping.is_set():
                report(b"ERROR:input\n")
            parent_closed.set()
            stopping.set()

    def console_writer():
        try:
            while not stopping.is_set():
                with input_condition:
                    if not pending_input:
                        input_condition.wait(timeout=PUMP_WAIT_SECONDS)
                        continue
                    data = bytes(pending_input[:IO_CHUNK_BYTES])
                    del pending_input[:IO_CHUNK_BYTES]
                console.write(data)
                report(f"DRAIN:{len(data)}\n".encode("ascii"))
        except OSError:
            stopping.set()

    def output_loop():
        try:
            while True:
                while not credit.wait(timeout=PUMP_WAIT_SECONDS):
                    if parent_closed.is_set():
                        break
                credit.clear()
                data = console.read()
                if not data:
                    stopping.set()
                    return
                if len(data) > IO_CHUNK_BYTES:
                    raise OSError("Oversized console output chunk")
                if not parent_closed.is_set():
                    write_output(HEADER.pack(b"O", len(data)) + data)
        except OSError:
            stopping.set()

    report(f"READY:{console.pid}\n".encode("ascii"))
    controls = threading.Thread(target=input_loop, daemon=True, name="conpty-controls")
    writer = threading.Thread(target=console_writer, daemon=True, name="conpty-input")
    reader = threading.Thread(target=output_loop, daemon=True, name="conpty-output")
    reader.start()
    controls.start()
    writer.start()
    terminated = False
    try:
        while (code := console.poll()) is None:
            with input_condition:
                size = pending_size.pop() if pending_size else None
            if size is not None and not stopping.is_set():
                console.resize(*size)
            if stopping.is_set() and not terminated:
                console.terminate()
                terminated = True
        stopping.set()
        # The root handle is no longer being waited on. A separate reader
        # continues to drain the final console frame during ClosePseudoConsole.
        console.close()
        writer.join(timeout=INPUT_JOIN_TIMEOUT_SECONDS)
        reader.join(timeout=OUTPUT_DRAIN_TIMEOUT_SECONDS)
        if reader.is_alive():
            report(b"ERROR:drain\n")
            return 1
        console.close_output()
        report(f"EXIT:{code}\n".encode("ascii"))
        return 0
    finally:
        stopping.set()


def main() -> int:
    """Private inherited pipes only: no TCP listener, shell parsing, or Qt imports."""
    if sys.platform != "win32":
        _write_all(2, b"ERROR:unsupported\n")
        return 1
    import msvcrt

    from termana.terminal.windows_conpty import WindowsConsole

    report_lock = threading.Lock()

    def report(data: bytes) -> None:
        with report_lock:
            _write_all(2, data)

    try:
        for fd in (0, 1, 2):
            msvcrt.setmode(fd, os.O_BINARY)
        packet = read_frame(lambda count: os.read(0, count))
        if packet is None or packet[0] != b"L":
            raise ValueError("Missing console launch")
        argv, cwd, columns, rows = parse_launch(packet[1])
        console = WindowsConsole(argv, cwd, columns, rows)
        return run_console(
            console,
            lambda count: os.read(0, count),
            lambda data: _write_all(1, data),
            report,
        )
    except (OSError, ValueError):
        # Do not copy argv, environment capabilities or arbitrary native errors
        # into application guidance. Raw terminal output stays on stdout only.
        report(b"ERROR:startup\n")
        return 1


if __name__ == "__main__":
    # Input can still be blocked in a synchronous pipe read. OS process teardown
    # closes the private job handle and all remaining pipes, including on failure.
    os._exit(main())
