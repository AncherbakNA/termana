"""Public GitHub metadata checks with no tokens, extraction or installation."""

from __future__ import annotations

import hashlib
import hmac
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from termana.updates.manifest import (
    MAX_MANIFEST_BYTES,
    ReleaseRepository,
    UpdateArtifact,
    UpdateError,
    VerifiedManifest,
    read_signed_manifest,
    validate_channel,
    version_key,
)

_ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)
_READ_CHUNK = 64 * 1024
MAX_HTTP_METADATA_BYTES = 256 * 1024


def _validate_url(url: str) -> None:
    if type(url) is not str or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise UpdateError("Unsafe update download URL")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in _ALLOWED_HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.fragment
        ):
            raise UpdateError("Update download destination is not allowed")
    except ValueError:
        raise UpdateError("Unsafe update download URL") from None


class _SafeRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public_metadata(url: str, limit: int) -> bytes:
    """Bounded HTTPS GET. Redirects cannot downgrade TLS or leave GitHub assets.

    Synchronous worker/CLI primitive, NEVER call from the GUI thread. There is
    no authentication/cookie handler and no Authorization header. TLS validation
    uses Python's default verified HTTPS context, including on CDN redirects.
    """
    _validate_url(url)
    if type(limit) is not int or not 1 <= limit <= MAX_HTTP_METADATA_BYTES:
        raise UpdateError("Invalid update metadata read limit")
    headers = {
        "Accept": "application/octet-stream",
        "Accept-Encoding": "identity",
        "User-Agent": "Termana-Update-Check/1",
    }
    if urlsplit(url).hostname == "api.github.com":
        headers.update(
            {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        )
    request = Request(url, headers=headers)
    deadline = time.monotonic() + 30
    try:
        with build_opener(_SafeRedirects()).open(request, timeout=10) as response:
            _validate_url(response.geturl())
            if response.status != 200:
                raise UpdateError("Update server did not return metadata")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise UpdateError("Compressed update metadata is not accepted")
            length = response.headers.get("Content-Length")
            if length is not None and (
                not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > limit
            ):
                raise UpdateError("Update metadata exceeds its size limit")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise UpdateError("Update metadata request timed out")
                chunk = response.read(min(_READ_CHUNK, limit + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UpdateError("Update metadata exceeds its size limit")
                chunks.append(chunk)
            if length is not None and size != int(length):
                raise UpdateError("Update metadata was truncated")
            return b"".join(chunks)
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 404:
            raise UpdateError("Update feed is not published or is not public yet") from None
        if status in (403, 429):
            raise UpdateError(
                "Update server temporarily refused the request; retry later"
            ) from None
        raise UpdateError("Update metadata request failed") from None
    except (URLError, OSError) as error:
        if isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError):
            raise UpdateError(
                "GitHub's certificate could not be verified. Check your clock and system trusted certificates; TLS verification remains enabled."
            ) from None
        raise UpdateError("Could not securely reach the update server") from None


@dataclass(frozen=True, slots=True)
class UpdateCheckpoint:
    """Caller-owned last accepted feed state; must eventually be stored durably.

    Never populate this from an unverified response. Persistence/GUI integration
    is a later block, so this module alone cannot claim durable replay protection.
    """

    repository: ReleaseRepository
    channel: str
    sequence: int
    sha256: str

    def __post_init__(self) -> None:
        validate_channel(self.channel)
        if (
            type(self.repository) is not ReleaseRepository
            or type(self.sequence) is not int
            or not 1 <= self.sequence <= 2**53 - 1
        ):
            raise UpdateError("Invalid trusted update checkpoint")
        if type(self.sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise UpdateError("Invalid trusted update checkpoint digest")


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    status: str
    manifest: VerifiedManifest
    artifact: UpdateArtifact | None
    checkpoint: UpdateCheckpoint


def validate_check_request(
    repository, public_key, channel, current_version, system, architecture, now, checkpoint
):
    """Reject invalid local inputs before either discovery or asset HTTP calls."""
    validate_channel(channel)
    current = version_key(current_version)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise UpdateError("An aware current time is required")
    if type(repository) is not ReleaseRepository:
        raise UpdateError("A trusted release repository is required")
    if type(public_key) is not bytes or len(public_key) != 32:
        raise UpdateError("A pinned Ed25519 public key is required")
    if (
        type(system) is not str
        or system not in {"macos", "windows", "linux"}
        or type(architecture) is not str
        or architecture not in {"arm64", "x86_64"}
    ):
        raise UpdateError("Unsupported update platform or architecture")
    if checkpoint is not None and (
        type(checkpoint) is not UpdateCheckpoint
        or checkpoint.repository != repository
        or checkpoint.channel != channel
    ):
        raise UpdateError("Trusted update checkpoint belongs to another feed")
    return current


def check_for_update(
    repository: ReleaseRepository,
    public_key: bytes,
    *,
    channel: str,
    current_version: str,
    system: str,
    architecture: str,
    now: datetime,
    checkpoint: UpdateCheckpoint | None = None,
    fetch: Callable[[str, int], bytes] = fetch_public_metadata,
    release_tag: str | None = None,
) -> UpdateCheckResult:
    """Verify one explicit versioned release, reject replay, choose exact CPU.

    The source launcher is untouched. Even AVAILABLE only describes a candidate:
    no package download/execution/install, minimum-OS/schema compatibility proof,
    active-session shutdown, new key trust or persistent-state mutation occurs.
    """
    current = validate_check_request(
        repository, public_key, channel, current_version, system, architecture, now, checkpoint
    )
    if release_tag is None:
        raise UpdateError("Use immutable release discovery or supply a versioned release tag")
    tag = release_tag
    if release_tag is not None:
        if type(tag) is not str or not tag.startswith("v"):
            raise UpdateError("Invalid immutable release tag")
        version_key(tag[1:])
    payload = fetch(repository.asset_url(tag, "manifest.json"), MAX_MANIFEST_BYTES)
    signature = fetch(repository.asset_url(tag, "manifest.sig"), 64)
    manifest = read_signed_manifest(
        payload,
        signature,
        public_key,
        repository=repository,
        channel=channel,
        now=now,
    )
    if release_tag is not None and manifest.version != tag[1:]:
        raise UpdateError("Signed version does not match the immutable release tag")
    if checkpoint is not None:
        if manifest.sequence < checkpoint.sequence:
            raise UpdateError("Update metadata rollback was rejected")
        if manifest.sequence == checkpoint.sequence and manifest.sha256 != checkpoint.sha256:
            raise UpdateError("Update metadata changed without a new sequence")
    accepted = UpdateCheckpoint(repository, channel, manifest.sequence, manifest.sha256)
    artifact = manifest.artifact_for(system, architecture)
    if version_key(manifest.version) <= current:
        status = "up-to-date"
    elif artifact is None:
        status = "no-compatible-package"
    else:
        status = "available"
    return UpdateCheckResult(status, manifest, artifact, accepted)


def verify_package(stream: BinaryIO, artifact: UpdateArtifact) -> None:
    """Check an already-opened stream against authenticated size/hash; no writes.

    A future installer must retain ownership of these same bytes and separately
    verify the platform updater signature, not reopen an untrusted mutable path.
    """
    if type(artifact) is not UpdateArtifact:
        raise UpdateError("Verified asset metadata is required")
    digest, remaining = hashlib.sha256(), artifact.size
    while remaining:
        requested = min(_READ_CHUNK, remaining)
        chunk = stream.read(requested)
        if type(chunk) is not bytes or not chunk or len(chunk) > requested:
            raise UpdateError("Update package size does not match")
        digest.update(chunk)
        remaining -= len(chunk)
    if stream.read(1) != b"":
        raise UpdateError("Update package size does not match")
    if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
        raise UpdateError("Update package checksum does not match")
