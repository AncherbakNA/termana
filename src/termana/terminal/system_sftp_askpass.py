"""Standalone OpenSSH response helper. No Qt, package imports or secret storage.

Only this child inherits the short-lived random loopback capability. The SSH
prompt is never interpolated into a shell command, logged, or treated as code.
Host trust is strict in the parent command; confirmations are refused here.
"""

import json
import os
import socket
import sys

CAPABILITY_ENV = "TERMANA_SYSTEM_SFTP_RESPONSE"
MAX_RESPONSE_BYTES = 1023
MAX_MESSAGE_BYTES = 8192


def main():
    try:
        if os.environ.get("SSH_ASKPASS_PROMPT", "") not in ("",):
            return 1
        if len(sys.argv) != 2 or len(sys.argv[1].encode("utf-8")) > 4096:
            return 1
        capability = json.loads(os.environ.pop(CAPABILITY_ENV))
        port, token = capability["port"], capability["token"]
        if type(port) is not int or not 0 < port < 65536 or len(token) != 64:
            return 1
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.settimeout(120)
            request = (
                json.dumps({"token": token, "prompt": sys.argv[1]}, ensure_ascii=False).encode(
                    "utf-8"
                )
                + b"\n"
            )
            if len(request) > MAX_MESSAGE_BYTES:
                return 1
            client.sendall(request)
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = client.recv(min(1024, MAX_MESSAGE_BYTES + 1 - len(data)))
                if not chunk or len(data) + len(chunk) > MAX_MESSAGE_BYTES:
                    return 1
                data.extend(chunk)
        response = json.loads(data)
        value = response.get("answer")
        if type(value) is not str or any(c in value for c in "\r\n\0"):
            return 1
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            return 1
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
