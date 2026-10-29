"""Explicit endpoint-bound RDP certificate pins, separate from SSH known_hosts.

Reuses only the audited private-file snapshot/guarded-commit primitives. No SSH
records/parser are used. Reads never create or repair a directory or trust item.
"""
import base64
import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from termana.credentials.embedded_host_trust import _commit
from termana.rdp.certificate import RdpCertificate, decode_certificate
from termana.rdp.endpoint import RdpEndpoint
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.terminal.private_recovery_files import PrivateDirectory, PrivateFile

MAX_TRUST_BYTES = 1024 * 1024


class RdpTrustError(ValueError):
    def __init__(self):
        super().__init__("Saved RDP certificate trust could not be confirmed safely")


@dataclass(frozen=True, slots=True)
class TrustReview:
    host: str
    port: int
    fingerprint: str | None
    matches: bool
    data: bytes = field(repr=False)
    status: object = field(repr=False)


def _document(data):
    if not data:
        return {"schema_version": 1, "certificates": []}
    document = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
    if (type(document) is not dict or set(document) != {"schema_version", "certificates"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or type(document["certificates"]) is not list or len(document["certificates"]) > 512):
        raise RdpTrustError()
    endpoints = set()
    for entry in document["certificates"]:
        if type(entry) is not dict or set(entry) != {"host", "port", "sha256", "der", "name_mismatch"}:
            raise RdpTrustError()
        RdpEndpoint(entry["host"], entry["port"])
        endpoint = entry["host"], entry["port"]
        if endpoint in endpoints or type(entry["name_mismatch"]) is not bool:
            raise RdpTrustError()
        endpoints.add(endpoint)
        if type(entry["der"]) is not str or len(entry["der"]) > 22000:
            raise RdpTrustError()
        der = base64.b64decode(entry["der"], validate=True)
        digest = entry["sha256"]
        if type(digest) is not str or len(digest) != 64:
            raise RdpTrustError()
        decode_certificate(struct.pack("!I", 128 if entry["name_mismatch"] else 0)
                           + digest.encode("ascii") + struct.pack("!I", len(der)) + der)
    return document


class RdpTrustStore:
    def __init__(self, path: Path):
        if not isinstance(path, Path) or not path.is_absolute():
            raise RdpTrustError()
        self.path = path

    def _snapshot(self):
        # Windows walks all ancestors with OBJ_DONT_REPARSE. POSIX O_NOFOLLOW
        # protects immediate objects; reject ancestor links before opening too.
        if os.name == "posix" and any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise RdpTrustError()
        try:
            with PrivateDirectory(self.path.parent) as directory:
                try:
                    with PrivateFile(directory, self.path.name) as opened:
                        return opened.read(MAX_TRUST_BYTES), os.fstat(opened.fd)
                except FileNotFoundError:
                    return b"", None
        except FileNotFoundError:
            # Only one application parent can later be created after consent.
            if not self.path.parent.parent.is_dir():
                raise RdpTrustError() from None
            return b"", None

    def review(self, host, port, certificate):
        try:
            RdpEndpoint(host, port)
            if type(certificate) is not RdpCertificate:
                raise RdpTrustError()
            data, status = self._snapshot()
            if status is not None and not data:
                raise RdpTrustError()  # Existing empty/truncated document is not a missing store.
            document = _document(data)
            previous = next((item for item in document["certificates"]
                             if (item["host"], item["port"]) == (host, port)), None)
            matches = bool(previous and previous["sha256"] == certificate.fingerprint
                           and previous["name_mismatch"] == certificate.name_mismatch
                           and certificate.remember_allowed)
            return TrustReview(host, port, previous["sha256"] if previous else None, matches, data, status)
        except Exception:
            raise RdpTrustError() from None

    def remember(self, review, certificate):
        try:
            if type(review) is not TrustReview or type(certificate) is not RdpCertificate or not certificate.remember_allowed:
                raise RdpTrustError()
            document = _document(review.data)
            entries = [item for item in document["certificates"]
                       if (item["host"], item["port"]) != (review.host, review.port)]
            entries.append(dict(host=review.host, port=review.port, sha256=certificate.fingerprint,
                                der=base64.b64encode(certificate.der).decode("ascii"),
                                name_mismatch=certificate.name_mismatch))
            if len(entries) > 512:
                raise RdpTrustError()
            data = json.dumps(dict(schema_version=1, certificates=entries), ensure_ascii=True).encode()
            if len(data) > MAX_TRUST_BYTES:
                raise RdpTrustError()
            # _commit checks both bytes and exact snapshot metadata again and
            # creates private stage/parent only after this explicit consent.
            # As with existing SSH policy, replacement is guarded, not a general
            # interprocess compare-and-swap; app single-instance lock still applies.
            _commit(self.path, review, data, create_parent=not self.path.parent.exists())
        except Exception:
            raise RdpTrustError() from None
