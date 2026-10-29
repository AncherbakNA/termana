"""Explicit offline release metadata signing; never uploads or creates keys."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from termana.terminal.private_recovery_files import PrivateDirectory, PrivateFile
from termana.updates.manifest import UpdateArtifact, read_signed_manifest
from termana.updates.official import PUBLIC_KEY, REPOSITORY


def artifact_record(system, architecture, package, path):
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Choose a regular absolute release asset")
    before = path.stat()
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("Release asset changed while hashing")
    item = UpdateArtifact(system, architecture, package, path.name, before.st_size, checksum)
    return {name: getattr(item, name) for name in item.__dataclass_fields__}


def signed_metadata(seed, artifacts, *, version, sequence, notes, now, expected_key=PUBLIC_KEY):
    signing = Ed25519PrivateKey.from_private_bytes(seed)
    public = signing.public_key().public_bytes_raw()
    if public != expected_key:
        raise ValueError("Publisher key does not match the application's trust anchor")

    def timestamp(value):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    document = dict(
        format="termana-update",
        schema_version=1,
        repository=REPOSITORY.identity,
        channel="stable",
        sequence=sequence,
        version=version,
        published_at=timestamp(now),
        expires_at=timestamp(now + timedelta(days=90)),
        notes=notes,
        artifacts=artifacts,
    )
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    signature = signing.sign(payload)
    # Same closed schema/expiry/tag rules the client uses, before any publication.
    read_signed_manifest(
        payload, signature, public, repository=REPOSITORY, channel="stable", now=now
    )
    return payload, signature


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument(
        "--artifact",
        nargs=4,
        action="append",
        required=True,
        metavar=("SYSTEM", "ARCH", "FORMAT", "ABSOLUTE_FILE"),
    )
    args = parser.parse_args()
    for path in (args.key_directory, args.output):
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            parser.error("Use absolute, unlinked private key/output paths")
    with (
        PrivateDirectory(args.key_directory) as root,
        PrivateFile(root, "update-signing.key") as key,
    ):
        seed = key.read(32)
    artifacts = [artifact_record(a, b, c, Path(d)) for a, b, c, d in args.artifact]
    notes = args.notes_file.read_text(encoding="utf-8")
    payload, signature = signed_metadata(
        seed,
        artifacts,
        version=args.version,
        sequence=args.sequence,
        notes=notes,
        now=datetime.now(UTC),
    )
    with PrivateDirectory(args.output, create=True, writable=True) as root:
        for name, data in (
            ("manifest.json", payload),
            ("manifest.sig", signature),
            ("update-public-key.bin", PUBLIC_KEY),
        ):
            with PrivateFile(root, name, create=True) as target:
                target.rewrite(data)
        root.sync()
    print(
        json.dumps(
            {
                "signed": True,
                "version": args.version,
                "sequence": args.sequence,
                "assets": len(artifacts),
                "manifest_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
