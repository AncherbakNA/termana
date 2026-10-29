"""Explicit Telnet warning choices, endpoint-bound and separate from trust/secrets."""

import ipaddress
import json
from pathlib import Path

from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.storage.atomic_file import replace_file_atomically
from termana.storage.json_objects import reject_duplicate_json_keys

MAX_ENDPOINTS = 1024
MAX_BYTES = 512 * 1024


class TelnetWarningStoreError(RuntimeError):
    """A saved warning choice could not be read or published safely."""


def endpoint_key(host: str, port: int) -> tuple[str, int]:
    """Validate and canonicalize exactly one Telnet destination."""
    ConnectionProfile("Telnet warning", host, ConnectionProtocol.TELNET, port=port)
    try:
        host = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    return host, port


class TelnetWarningStore:
    """Persist only explicit endpoint choices, never authentication material."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> set[tuple[str, int]]:
        try:
            if self.path.is_symlink():
                raise ValueError("Symlink warning store refused")
            try:
                with self.path.open("rb") as stream:
                    raw = stream.read(MAX_BYTES + 1)
            except FileNotFoundError:
                return set()
            if len(raw) > MAX_BYTES:
                raise ValueError("Warning store too large")
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
            if (
                type(data) is not dict
                or set(data) != {"schema_version", "endpoints"}
                or type(data["schema_version"]) is not int
                or data["schema_version"] != 1
                or type(data["endpoints"]) is not list
                or len(data["endpoints"]) > MAX_ENDPOINTS
            ):
                raise ValueError("Unsupported warning choices")
            entries = set()
            for row in data["endpoints"]:
                if type(row) is not list or len(row) != 2:
                    raise ValueError("Invalid warning endpoint")
                key = endpoint_key(*row)
                if list(key) != row or key in entries:
                    raise ValueError("Invalid or duplicate warning endpoint")
                entries.add(key)
            return entries
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise TelnetWarningStoreError("Could not read Telnet warning choices safely") from error

    def is_remembered(self, host: str, port: int) -> bool:
        return endpoint_key(host, port) in self._load()

    def remember(self, host: str, port: int) -> None:
        entries = self._load()
        entries.add(endpoint_key(host, port))
        if len(entries) > MAX_ENDPOINTS:
            raise TelnetWarningStoreError("Too many remembered Telnet warning choices")
        self._save(entries)

    def clear(self) -> None:
        self._load()  # Never overwrite an unknown/corrupt document silently.
        self._save(set())

    def _save(self, entries: set[tuple[str, int]]) -> None:
        try:
            raw = json.dumps(
                dict(schema_version=1, endpoints=sorted(entries)), ensure_ascii=False
            ).encode("utf-8")
            if len(raw) > MAX_BYTES or self.path.is_symlink():
                raise ValueError("Unsafe warning destination")
            replace_file_atomically(self.path, raw + b"\n")
        except (OSError, ValueError) as error:
            raise TelnetWarningStoreError("Could not save Telnet warning choices") from error
