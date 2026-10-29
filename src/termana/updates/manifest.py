"""Strict signed release metadata, independent of Qt and private app stores.

The publisher key is supplied by the trusted caller, never by downloaded JSON.
This is Termana's metadata protocol, not a replacement for Sparkle/WinSparkle's
native installation/signature protocol. No production signing key is included.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import quote

MAX_MANIFEST_BYTES: Final = 32 * 1024
MAX_PACKAGE_BYTES: Final = 2 * 1024**3 - 1
MAX_ARTIFACTS: Final = 16
MAX_MANIFEST_LIFETIME: Final = timedelta(days=90)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(
    r"(0|[1-9][0-9]{0,7})\.(0|[1-9][0-9]{0,7})\.(0|[1-9][0-9]{0,7})"
    r"(?:-qa\.([1-9][0-9]{0,7}))?"
)
_FORMATS: Final = {
    "macos": {"zip": ".zip", "dmg": ".dmg"},
    "windows": {"exe": ".exe"},
    "linux": {"tar.gz": ".tar.gz", "appimage": ".AppImage"},
}


class UpdateError(ValueError):
    """A safe, fixed diagnostic, without server payloads or private paths."""


@dataclass(frozen=True, slots=True)
class ReleaseRepository:
    """An explicit GitHub.com repository, not an arbitrary URL or local path."""

    owner: str
    name: str

    def __post_init__(self) -> None:
        if (
            type(self.owner) is not str
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", self.owner)
            or "--" in self.owner
        ):
            raise UpdateError("Invalid GitHub repository owner")
        if (
            type(self.name) is not str
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", self.name)
            or self.name.endswith(".git")
        ):
            raise UpdateError("Invalid GitHub repository name")

    @classmethod
    def parse(cls, value: str) -> ReleaseRepository:
        if type(value) is not str or value.count("/") != 1:
            raise UpdateError("Use a GitHub repository in owner/name form")
        return cls(*value.split("/"))

    @property
    def identity(self) -> str:
        return f"{self.owner}/{self.name}"

    def asset_url(self, tag: str, name: str) -> str:
        return (
            f"https://github.com/{self.identity}/releases/download/"
            f"{quote(tag, safe='')}/{quote(name, safe='')}"
        )


def version_key(value: str) -> tuple[int, int, int, int, int]:
    """Compare the initial supported versions: M.N.P and M.N.P-qa.N only."""
    matched = _VERSION.fullmatch(value) if type(value) is str else None
    if matched is None:
        raise UpdateError("Unsupported release version; use M.N.P or M.N.P-qa.N")
    major, minor, patch = (int(matched[i]) for i in (1, 2, 3))
    qa = matched[4]
    return major, minor, patch, int(qa is None), 0 if qa is None else int(qa)


def validate_channel(channel: str) -> None:
    if type(channel) is not str or channel not in {"stable", "qa"}:
        raise UpdateError("Update channel must be stable or qa")


@dataclass(frozen=True, slots=True)
class UpdateArtifact:
    system: str
    architecture: str
    package: str
    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.system) is not str or self.system not in _FORMATS:
            raise UpdateError("Unsupported update operating system")
        if type(self.architecture) is not str or self.architecture not in {"x86_64", "arm64"}:
            raise UpdateError("Unsupported update architecture")
        formats = _FORMATS[self.system]
        if type(self.package) is not str or self.package not in formats:
            raise UpdateError("Unsupported update package format")
        if (
            type(self.name) is not str
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", self.name)
            or not self.name.endswith(formats[self.package])
        ):
            raise UpdateError("Unsafe or mismatched update asset name")
        if type(self.size) is not int or not 1 <= self.size <= MAX_PACKAGE_BYTES:
            raise UpdateError("Invalid update asset size")
        if type(self.sha256) is not str or _DIGEST.fullmatch(self.sha256) is None:
            raise UpdateError("Invalid update asset checksum")


@dataclass(frozen=True, slots=True)
class VerifiedManifest:
    """Metadata returned only after verification by read_signed_manifest.

    Callers must not use manually constructed records as a verification result.
    """

    repository: ReleaseRepository
    channel: str
    sequence: int
    version: str
    published_at: datetime
    expires_at: datetime
    notes: str
    artifacts: tuple[UpdateArtifact, ...]
    sha256: str

    def artifact_for(self, system: str, architecture: str) -> UpdateArtifact | None:
        return next(
            (
                item
                for item in self.artifacts
                if item.system == system and item.architecture == architecture
            ),
            None,
        )

    def package_url(self, artifact: UpdateArtifact) -> str:
        if artifact not in self.artifacts:
            raise UpdateError("Asset does not belong to the verified release")
        return self.repository.asset_url(f"v{self.version}", artifact.name)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("Duplicate update metadata field")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise UpdateError("Nonstandard JSON number in update metadata")


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        raise UpdateError("Update timestamps must be UTC seconds")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise UpdateError("Invalid update timestamp") from None


def read_signed_manifest(
    payload: bytes,
    signature: bytes,
    public_key: bytes,
    *,
    repository: ReleaseRepository,
    channel: str,
    now: datetime,
) -> VerifiedManifest:
    """Verify exact wire bytes BEFORE parsing, then enforce a closed v1 schema."""
    validate_channel(channel)
    if type(repository) is not ReleaseRepository:
        raise UpdateError("A trusted release repository is required")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise UpdateError("An aware current time is required")
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_MANIFEST_BYTES:
        raise UpdateError("Update metadata exceeds its size limit")
    if type(signature) is not bytes or len(signature) != 64:
        raise UpdateError("Invalid update signature length")
    if type(public_key) is not bytes or len(public_key) != 32:
        raise UpdateError("A pinned Ed25519 public key is required")
    # Already provided by the installed SSH dependency; do not install/upgrade it.
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise UpdateError("Update signature verification is unavailable") from None
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, UnsupportedAlgorithm, ValueError):
        raise UpdateError("Update signature verification failed") from None
    try:
        data = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        raise UpdateError("Invalid signed update JSON") from None
    fields = {
        "format",
        "schema_version",
        "repository",
        "channel",
        "sequence",
        "version",
        "published_at",
        "expires_at",
        "notes",
        "artifacts",
    }
    if type(data) is not dict or set(data) != fields:
        raise UpdateError("Unsupported update metadata fields")
    if (
        data["format"] != "termana-update"
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
    ):
        raise UpdateError("Unsupported update metadata format")
    if data["repository"] != repository.identity or data["channel"] != channel:
        raise UpdateError("Update repository or channel does not match")
    if type(data["sequence"]) is not int or not 1 <= data["sequence"] <= 2**53 - 1:
        raise UpdateError("Invalid update sequence")
    key = version_key(data["version"])
    if bool(key[3]) != (channel == "stable"):
        raise UpdateError("Release version does not match its channel")
    published, expires = _timestamp(data["published_at"]), _timestamp(data["expires_at"])
    if not published < expires or expires - published > MAX_MANIFEST_LIFETIME:
        raise UpdateError("Invalid update metadata lifetime")
    if published > now + timedelta(minutes=5) or expires <= now:
        raise UpdateError("Update metadata expired or system clock is incorrect")
    notes = data["notes"]
    if (
        type(notes) is not str
        or len(notes) > 2048
        or any(
            (ord(char) < 32 and char not in "\n\t")
            or ord(char) == 127
            or char in "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
            for char in notes
        )
    ):
        raise UpdateError("Invalid update release notes")
    raw_artifacts = data["artifacts"]
    if type(raw_artifacts) is not list or not 1 <= len(raw_artifacts) <= MAX_ARTIFACTS:
        raise UpdateError("Invalid update asset list")
    artifacts = []
    targets, names = set(), set()
    artifact_fields = {"system", "architecture", "package", "name", "size", "sha256"}
    for item in raw_artifacts:
        if type(item) is not dict or set(item) != artifact_fields:
            raise UpdateError("Unsupported update asset fields")
        artifact = UpdateArtifact(**item)
        target = (artifact.system, artifact.architecture)
        if target in targets or artifact.name in names:
            raise UpdateError("Duplicate update asset or target")
        targets.add(target)
        names.add(artifact.name)
        artifacts.append(artifact)
    return VerifiedManifest(
        repository,
        channel,
        data["sequence"],
        data["version"],
        published,
        expires,
        notes,
        tuple(artifacts),
        hashlib.sha256(payload).hexdigest(),
    )
