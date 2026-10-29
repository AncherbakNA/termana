"""Scoped known-host record rewriting; all host/hash matching belongs to AsyncSSH."""

import re

import asyncssh

from termana.credentials.embedded_ssh_trust import MAX_EMBEDDED_TRUST_FILE_BYTES


def replace_host_records(data: bytes, host: str, addr: str, port: int, key) -> bytes | None:
    """Keep aliases on their old key, preserve hashed identity and policy markers.

    Wildcard, negated, address-only or port-fallback matches cannot be rewritten
    without changing other targets. Return None to offer explicit session-only.
    """
    token = host if port == 22 else f"[{host}]:{port}"
    retained, hashes, comments = [], [], []
    for line in data.splitlines(keepends=True):
        known = asyncssh.import_known_hosts(line.decode("utf-8"))
        if not known.match(host, addr, None if port == 22 else port)[0]:
            retained.append(line)
            continue
        record = re.fullmatch(rb"(\s*)(\S+)(.*)", line, re.DOTALL)
        if record is None:
            return None
        prefix, pattern, suffix = record.groups()
        if pattern.startswith(b"|1|"):
            if not known.match(host, "", None if port == 22 else port)[0]:
                return None
            # Don't replace an unqualified hash used as fallback for another
            # port: test exact matching through a lookup-only anchor.
            if port != 22:
                anchored = asyncssh.import_known_hosts(
                    line.decode("utf-8") + "\n" + token + " " + key.export_public_key().decode()
                ).match(host, "", port)[0]
                if not any(item.public_data != key.public_data for item in anchored):
                    return None
            hashes.append(pattern)
        else:
            names = pattern.split(b",")
            if any(any(char in name for char in b"*?!/") for name in names):
                return None
            if token.encode() not in names:
                return None
            remaining = [name for name in names if name != token.encode()]
            if remaining:
                retained.append(prefix + b",".join(remaining) + suffix)
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[3].strip():
            comments.append(fields[3].strip())
    prefix = b"".join(retained)
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    public = b" ".join(key.export_public_key("openssh").split()[:2])
    comment = b" " + comments[0] if comments else b""
    # Keep an existing hash instead of exposing a previously hidden hostname.
    target = hashes[0] if hashes else token.encode("utf-8")
    result = prefix + target + b" " + public + comment + b"\n"
    return result if len(result) <= MAX_EMBEDDED_TRUST_FILE_BYTES else None
