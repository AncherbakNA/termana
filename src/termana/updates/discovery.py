"""Read-only immutable release discovery; API metadata never establishes trust."""

import json

from termana.updates.client import (
    MAX_HTTP_METADATA_BYTES,
    check_for_update,
    fetch_public_metadata,
    validate_check_request,
)
from termana.updates.manifest import MAX_MANIFEST_BYTES, UpdateError, version_key

PAGE_SIZE = 10
MAX_PAGES = 10


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("Duplicate release discovery field")
        result[key] = value
    return result


def _constant(_value):
    raise UpdateError("Nonstandard release discovery JSON")


def _page(payload):
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_HTTP_METADATA_BYTES:
        raise UpdateError("Release discovery response exceeds its size limit")
    try:
        data = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise UpdateError("Invalid release discovery response") from None
    if (
        type(data) is not list
        or len(data) > PAGE_SIZE
        or any(type(item) is not dict for item in data)
    ):
        raise UpdateError("Invalid release discovery page")
    return data


def _assets(release):
    assets = release.get("assets")
    if type(assets) is not list or len(assets) > 64:
        raise UpdateError("Invalid immutable release asset list")
    result = {}
    for asset in assets:
        if type(asset) is not dict or type(asset.get("name")) is not str:
            raise UpdateError("Invalid immutable release asset")
        name = asset["name"]
        if name in result:
            raise UpdateError("Duplicate immutable release asset")
        result[name] = asset
    for name, maximum in (("manifest.json", MAX_MANIFEST_BYTES), ("manifest.sig", 64)):
        asset = result.get(name, {})
        size = asset.get("size")
        if (
            asset.get("state") != "uploaded"
            or type(size) is not int
            or not 1 <= size <= maximum
            or (name.endswith(".sig") and size != 64)
        ):
            raise UpdateError("Immutable release is missing complete signed metadata")
    return result


def discover_update(
    repository,
    public_key,
    *,
    channel,
    current_version,
    system,
    architecture,
    now,
    checkpoint=None,
    fetch=fetch_public_metadata,
):
    """Select highest version, not API ordering/latest; never rewrite release assets.

    Explicit cap: 100 releases scanned. An incomplete scan is an error, not a
    false up-to-date result. No fallback after invalid selected metadata/signature.
    Native installer/OS/schema gates and durable checkpoints remain separate.
    """
    validate_check_request(
        repository, public_key, channel, current_version, system, architecture, now, checkpoint
    )
    candidates, seen_tags = [], set()
    for page in range(1, MAX_PAGES + 1):
        url = (
            f"https://api.github.com/repos/{repository.identity}/releases"
            f"?per_page={PAGE_SIZE}&page={page}"
        )
        releases = _page(fetch(url, MAX_HTTP_METADATA_BYTES))
        for release in releases:
            tag = release.get("tag_name")
            if type(tag) is not str or not tag.startswith("v"):
                continue
            try:
                key = version_key(tag[1:])
            except UpdateError:
                continue  # unrelated releases are not a supported Termana version
            if any(
                type(release.get(field)) is not bool
                for field in ("draft", "prerelease", "immutable")
            ):
                raise UpdateError("Invalid release discovery flags")
            if release["draft"] or not release["immutable"]:
                continue
            qa = not bool(key[3])
            if release["prerelease"] != qa:
                raise UpdateError("Release tag and prerelease flag disagree")
            if qa != (channel == "qa"):
                continue
            if tag in seen_tags:
                raise UpdateError("Duplicate discovered release tag")
            seen_tags.add(tag)
            candidates.append((key, release))
        if len(releases) < PAGE_SIZE:
            break
    else:
        raise UpdateError("Release discovery limit reached; cannot prove newest version")
    if not candidates:
        raise UpdateError("No published immutable release is available for this channel")
    _, selected = max(candidates, key=lambda item: item[0])
    assets = _assets(selected)

    def bound_fetch(url, limit):
        content = fetch(url, limit)
        name = "manifest.sig" if url.endswith("/manifest.sig") else "manifest.json"
        if type(content) is not bytes or len(content) != assets[name]["size"]:
            raise UpdateError("Signed release metadata size does not match")
        return content

    result = check_for_update(
        repository,
        public_key,
        channel=channel,
        current_version=current_version,
        system=system,
        architecture=architecture,
        now=now,
        checkpoint=checkpoint,
        fetch=bound_fetch,
        release_tag=selected["tag_name"],
    )
    for artifact in result.manifest.artifacts:
        asset = assets.get(artifact.name, {})
        if (
            asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int
            or asset["size"] != artifact.size
        ):
            raise UpdateError("Signed package is missing or differs from its release")
        digest = asset.get("digest")
        if digest is not None and digest != f"sha256:{artifact.sha256}":
            raise UpdateError("Release package digest differs from signed metadata")
    return result
