"""Windows-only ConPTY and kill-on-close job ownership. No Qt dependency.

The helper owns these handles. Create the client suspended, attach it to the job,
then resume: descendants cannot escape ownership during the startup interval.
See Microsoft Learn: creating-a-pseudoconsole-session and procthread/job-objects.
"""

import ctypes as c
import subprocess
import sys

from termana.terminal.conpty_protocol import IO_CHUNK_BYTES, validate_size

DWORD = c.c_uint32
BOOL = c.c_int32
HANDLE = c.c_void_p
SIZE_T = c.c_size_t

# Win32 API values, named as in the Windows SDK.
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x20016
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
STARTF_USESTDHANDLES = 0x100
EXTENDED_STARTUPINFO_PRESENT = 0x80000
CREATE_UNICODE_ENVIRONMENT = 0x400
CREATE_SUSPENDED = 0x4
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
DWORD_MAX = 0xFFFFFFFF
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_OPERATION_ABORTED = 995

MAX_ATTRIBUTE_LIST_BYTES = 64 * 1024
PROCESS_POLL_TIMEOUT_MS = 50


class Coord(c.Structure):
    _fields_ = [("X", c.c_int16), ("Y", c.c_int16)]


class StartupInfo(c.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", c.c_wchar_p),
        ("lpDesktop", c.c_wchar_p),
        ("lpTitle", c.c_wchar_p),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", c.c_uint16),
        ("cbReserved2", c.c_uint16),
        ("lpReserved2", HANDLE),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class StartupInfoEx(c.Structure):
    _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", HANDLE)]


class ProcessInformation(c.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class BasicJobLimits(c.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", c.c_int64),
        ("PerJobUserTimeLimit", c.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IoCounters(c.Structure):
    _fields_ = [
        (name, c.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class ExtendedJobLimits(c.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicJobLimits),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


def _kernel():
    if sys.platform != "win32":
        raise OSError("ConPTY requires Windows 10 version 1809 or newer")
    kernel = c.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreatePipe": (BOOL, [c.POINTER(HANDLE), c.POINTER(HANDLE), HANDLE, DWORD]),
        "CreatePseudoConsole": (c.c_int32, [Coord, HANDLE, HANDLE, DWORD, c.POINTER(HANDLE)]),
        "ResizePseudoConsole": (c.c_int32, [HANDLE, Coord]),
        "ClosePseudoConsole": (None, [HANDLE]),
        "CloseHandle": (BOOL, [HANDLE]),
        "InitializeProcThreadAttributeList": (BOOL, [HANDLE, DWORD, DWORD, c.POINTER(SIZE_T)]),
        "UpdateProcThreadAttribute": (
            BOOL,
            [HANDLE, DWORD, SIZE_T, HANDLE, SIZE_T, HANDLE, HANDLE],
        ),
        "DeleteProcThreadAttributeList": (None, [HANDLE]),
        "CreateProcessW": (
            BOOL,
            [
                c.c_wchar_p,
                c.c_wchar_p,
                HANDLE,
                HANDLE,
                BOOL,
                DWORD,
                HANDLE,
                c.c_wchar_p,
                c.POINTER(StartupInfoEx),
                c.POINTER(ProcessInformation),
            ],
        ),
        "CreateJobObjectW": (HANDLE, [HANDLE, c.c_wchar_p]),
        "SetInformationJobObject": (BOOL, [HANDLE, c.c_int32, HANDLE, DWORD]),
        "AssignProcessToJobObject": (BOOL, [HANDLE, HANDLE]),
        "TerminateJobObject": (BOOL, [HANDLE, DWORD]),
        "TerminateProcess": (BOOL, [HANDLE, DWORD]),
        "ResumeThread": (DWORD, [HANDLE]),
        "WaitForSingleObject": (DWORD, [HANDLE, DWORD]),
        "GetExitCodeProcess": (BOOL, [HANDLE, c.POINTER(DWORD)]),
        "ReadFile": (BOOL, [HANDLE, HANDLE, DWORD, c.POINTER(DWORD), HANDLE]),
        "WriteFile": (BOOL, [HANDLE, HANDLE, DWORD, c.POINTER(DWORD), HANDLE]),
    }
    for name, (result, args) in signatures.items():
        try:
            function = getattr(kernel, name)
        except AttributeError:
            raise OSError("This Windows version does not provide ConPTY") from None
        function.restype, function.argtypes = result, args
    return kernel


def _check(result) -> None:
    if not result:
        raise c.WinError(c.get_last_error())


class WindowsConsole:
    """Single-owner process resources; output must be drained during console close."""

    def __init__(self, argv: list[str], cwd: str, columns: int, rows: int) -> None:
        validate_size(columns, rows)
        self.kernel = _kernel()
        self.input = HANDLE()
        self.output = HANDLE()
        self.console = HANDLE()
        self.job = HANDLE()
        self.process = ProcessInformation()
        self._closed = False
        input_read, output_write = HANDLE(), HANDLE()
        attributes = None
        attributes_ready = False
        assigned = False
        try:
            _check(self.kernel.CreatePipe(c.byref(input_read), c.byref(self.input), None, 0))
            _check(self.kernel.CreatePipe(c.byref(self.output), c.byref(output_write), None, 0))
            result = self.kernel.CreatePseudoConsole(
                Coord(columns, rows), input_read, output_write, 0, c.byref(self.console)
            )
            if result < 0:
                raise OSError(f"CreatePseudoConsole failed (HRESULT {result & DWORD_MAX:08x})")
            size = SIZE_T()
            self.kernel.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
            if not 0 < size.value < MAX_ATTRIBUTE_LIST_BYTES:
                raise OSError("Invalid process attribute buffer size")
            attributes = c.create_string_buffer(size.value)
            _check(self.kernel.InitializeProcThreadAttributeList(attributes, 1, 0, c.byref(size)))
            attributes_ready = True
            _check(
                self.kernel.UpdateProcThreadAttribute(
                    attributes,
                    0,
                    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                    self.console,
                    c.sizeof(HANDLE),
                    None,
                    None,
                )
            )
            self.job = HANDLE(self.kernel.CreateJobObjectW(None, None))
            _check(self.job)
            limits = ExtendedJobLimits()
            # No breakaway: every descendant stays owned by this private job.
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(
                self.kernel.SetInformationJobObject(
                    self.job,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    c.byref(limits),
                    c.sizeof(limits),
                )
            )
            startup = StartupInfoEx()
            startup.StartupInfo.cb = c.sizeof(startup)
            # Explicitly clear the inherited standard-handle table. ConPTY
            # supplies the client's console handles; the helper's private Qt
            # pipes must not become the client's stdin/stdout/stderr. This is
            # also how Windows Terminal launches its attached ConPTY client.
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES  # Null standard handles.
            startup.lpAttributeList = c.cast(attributes, HANDLE)
            # CreateProcessW receives a writable buffer. There is no cmd.exe /c
            # intermediary, so metacharacters remain ordinary argv content.
            command_line = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            _check(
                self.kernel.CreateProcessW(
                    argv[0],
                    command_line,
                    None,
                    None,
                    False,
                    EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED,
                    None,
                    cwd,
                    c.byref(startup),
                    c.byref(self.process),
                )
            )
            _check(self.kernel.AssignProcessToJobObject(self.job, self.process.hProcess))
            assigned = True
            if self.kernel.ResumeThread(self.process.hThread) == DWORD_MAX:
                raise c.WinError(c.get_last_error())
        except BaseException:
            if self.process.hProcess and not assigned:
                self.kernel.TerminateProcess(self.process.hProcess, 1)
            # Startup failure has no reader thread yet. Close its output first
            # so ClosePseudoConsole cannot wait for a full undrained output pipe.
            self._close_handle(self.output)
            self.output = HANDLE()
            self.close()
            raise
        finally:
            if attributes_ready:
                self.kernel.DeleteProcThreadAttributeList(attributes)
            self._close_handle(input_read)
            self._close_handle(output_write)
            self._close_handle(self.process.hThread)
            self.process.hThread = None

    @property
    def pid(self) -> int:
        return int(self.process.dwProcessId)

    def read(self) -> bytes:
        buffer = c.create_string_buffer(IO_CHUNK_BYTES)
        count = DWORD()
        if not self.kernel.ReadFile(self.output, buffer, len(buffer), c.byref(count), None):
            if c.get_last_error() in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
                return b""
            raise c.WinError(c.get_last_error())
        return buffer.raw[: count.value]

    def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + IO_CHUNK_BYTES]
            buffer = c.create_string_buffer(chunk)
            count = DWORD()
            _check(self.kernel.WriteFile(self.input, buffer, len(chunk), c.byref(count), None))
            if count.value == 0:
                raise OSError("Console input pipe stopped accepting data")
            offset += count.value

    def resize(self, columns: int, rows: int) -> None:
        validate_size(columns, rows)
        if self.kernel.ResizePseudoConsole(self.console, Coord(columns, rows)) < 0:
            raise OSError("Could not resize the Windows console")

    def poll(self, timeout_ms: int = PROCESS_POLL_TIMEOUT_MS) -> int | None:
        result = self.kernel.WaitForSingleObject(self.process.hProcess, timeout_ms)
        if result == WAIT_TIMEOUT:
            return None
        if result != WAIT_OBJECT_0:
            raise c.WinError(c.get_last_error())
        code = DWORD()
        _check(self.kernel.GetExitCodeProcess(self.process.hProcess, c.byref(code)))
        return code.value

    def terminate(self) -> None:
        if self.job:
            _check(self.kernel.TerminateJobObject(self.job, 1))

    def close(self) -> None:
        """Call on a teardown thread, with a separate output reader still active."""
        if self._closed:
            return
        self._closed = True
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)
        if self.console:
            self.kernel.ClosePseudoConsole(self.console)
            self.console = HANDLE()
        # Input may still be used by the helper's daemon reader. Its OS handle
        # is released on helper exit, not concurrently with a pending WriteFile.
        self._close_handle(self.process.hProcess)
        self.process.hProcess = None
        self._close_handle(self.job)
        self.job = HANDLE()

    def close_output(self) -> None:
        self._close_handle(self.output)
        self.output = HANDLE()

    def _close_handle(self, handle) -> None:
        if handle:
            self.kernel.CloseHandle(handle)
