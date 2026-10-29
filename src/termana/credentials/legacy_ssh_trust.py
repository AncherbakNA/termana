"""Separate full-RSA1-key trust, with the existing guarded private-file writer.

Legacy MD5 is display-only. Identity comparisons use the complete representation
provided by the reviewed PuTTY frontend, never a truncated fingerprint.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from termana.credentials.embedded_host_trust import _commit, _snapshot
from termana.credentials.embedded_ssh_trust import EmbeddedTrustFileError
from termana.models.ssh_credential import SshEndpoint
from termana.storage.json_objects import reject_duplicate_json_keys

_KEY = re.compile(r"0x[0-9a-fA-F]+,0x[0-9a-fA-F]+\Z")
_FINGERPRINT = re.compile(r"(?:[0-9]{1,5} )?[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){15}\Z")


def validate_legacy_key(key, fingerprint):
    if (type(key) is not str or len(key) > 16384 or _KEY.fullmatch(key) is None
            or type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None):
        raise EmbeddedTrustFileError()


@dataclass(frozen=True)
class LegacyTrustReview:
    path: Path
    endpoint: SshEndpoint
    key: str
    fingerprint: str
    snapshot: object
    records: tuple
    previous: str | None
    trusted: bool

    @property
    def prompt(self):
        title = "SSH1 SERVER KEY CHANGED" if self.previous else "First SSH1 connection"
        return (
            f"{title}: {self.endpoint.host}:{self.endpoint.port}\n"
            f"RSA1 legacy MD5 fingerprint: {self.fingerprint}\n"
            + (f"Previously trusted: {self.previous}\n" if self.previous else "")
            + "SSH1 is insecure. Verify the fingerprint through a trusted channel.\n"
            "Accept and save the complete server key in Termana's separate SSH1 trust file? (yes/no)"
        )

    def commit(self):
        records = [r for r in self.records
                   if (r["host"], r["port"]) != (self.endpoint.host, self.endpoint.port)]
        records.append(dict(host=self.endpoint.host, port=self.endpoint.port,
                            key=self.key, fingerprint=self.fingerprint))
        data = (json.dumps(dict(version=1, keys=records), ensure_ascii=True,
                           sort_keys=True, indent=2) + "\n").encode("ascii")
        if len(data) > 1024 * 1024:
            raise EmbeddedTrustFileError()
        # The shared primitive validates owner/ACL, parent, unchanged bytes and
        # metadata, then stages a private atomic/no-clobber write on this OS.
        _commit(self.path, self.snapshot, data, create_parent=not self.path.parent.exists())


def review_legacy_key(path: Path, endpoint: SshEndpoint, key: str, fingerprint: str):
    validate_legacy_key(key, fingerprint)
    snapshot = _snapshot(path)
    try:
        document = (json.loads(snapshot.data, object_pairs_hook=reject_duplicate_json_keys)
                    if snapshot.data else dict(version=1, keys=[]))
        if (type(document) is not dict or set(document) != {"version", "keys"}
                or type(document["version"]) is not int or document["version"] != 1
                or type(document["keys"]) is not list or len(document["keys"]) > 4096):
            raise ValueError
        seen, previous, trusted = set(), None, False
        for record in document["keys"]:
            if type(record) is not dict or set(record) != {"host", "port", "key", "fingerprint"}:
                raise ValueError
            item = SshEndpoint(record["host"], record["port"])
            validate_legacy_key(record["key"], record["fingerprint"])
            identity = (item.host, item.port)
            if identity in seen:
                raise ValueError
            seen.add(identity)
            if item == endpoint:
                previous = record["fingerprint"]
                trusted = record["key"] == key
        return LegacyTrustReview(path, endpoint, key, fingerprint, snapshot,
                                 tuple(document["keys"]), previous, trusted)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise EmbeddedTrustFileError() from None
