"""Offline native component acceptance for source and frozen applications."""

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from termana.rdp.component import WINDOWS_CLIENT_SHA256, verified_component, windows_component_lease

EXPECTED = {
    "platform": {"platform": True, "connect": False},
    "crypto": {"crypto": "3.5.9", "sha256": True, "default_provider_only": True, "connect": False},
    "keymap": {"keymap": True, "connect": False},
    "paint": {"paint_coalesced": True, "connect": False},
    "settings": {"settings": True, "connect": False},
    "channels": {"channels": True, "connect": False},
    "registration": {"registration": True, "dynamic": 1, "actual": 2, "connect": False},
}


def check_component():
    result = {"passed": False, "connect": False, "user_data": False, "checks": {}}
    try:
        with ExitStack() as leases:
            executable = verified_component()
            if sys.platform == "win32":
                leases.enter_context(windows_component_lease(executable, WINDOWS_CLIENT_SHA256))
            environment = {k: v for k, v in os.environ.items()
                if not k.startswith(("DYLD_", "LD_", "WLOG_", "WINPR_", "FREERDP_"))
                and k not in {"SSLKEYLOGFILE", "OPENSSL_CONF", "OPENSSL_MODULES", "KRB5CCNAME"}}
            for name, expected in EXPECTED.items():
                child = subprocess.run([str(executable), f"--{name}-self-test"],
                    env=environment, capture_output=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                result["checks"][name] = (child.returncode == 0 and not child.stderr
                    and json.loads(child.stdout) == expected)
            result["passed"] = all(result["checks"].values())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        result["error"] = "Native RDP component verification failed"
    return result


def main(report_path):
    try:
        stream = Path(report_path).open("x", encoding="utf-8")
    except OSError:
        print("Choose a new report file in a writable folder.", file=sys.stderr)
        return 2
    with stream:
        result = check_component()
        json.dump(result, stream, indent=2)
    return 0 if result["passed"] else 1
