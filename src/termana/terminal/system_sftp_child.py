"""Windows-only isolated job owner for the binary OpenSSH subprocess tree."""

import ctypes
import subprocess
import sys
from pathlib import Path


def main():
    # The isolated interpreter adds only its own source root, not cwd/PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from termana.terminal.windows_conpty import (
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        ExtendedJobLimits,
        _kernel,
    )

    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return 1
    limits = ExtendedJobLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        return 1
    if not kernel.AssignProcessToJobObject(job, ctypes.c_void_p(-1)):
        return 1
    # Keep the sole noninheritable job handle until this wrapper exits. Killing
    # the wrapper closes it and terminates SSH, ProxyJump and response children.
    # Assigning self before spawning SSH removes the child-before-assignment race.
    return subprocess.call(sys.argv[1:])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1) from None
