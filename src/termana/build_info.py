"""Immutable package identity and intentionally non-personal support details."""

import json
import platform
import re
import sys
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files


def application_version() -> str:
    try:
        return version("termana")
    except PackageNotFoundError:
        return "development"


@cache
def packaged_build() -> dict[str, str]:
    """Read only build-generated package data, never settings/environment overrides."""
    fallback = {"build_id": "unknown" if getattr(sys, "frozen", False) else "source"}
    try:
        raw = files("termana").joinpath("build-info.json").read_bytes()
        if len(raw) > 4096:
            return fallback
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "build_id",
            "source_sha256",
        }:
            return fallback
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            return fallback
        if not isinstance(data["build_id"], str) or not re.fullmatch(
            r"[A-Za-z0-9._-]{1,100}", data["build_id"]
        ):
            return fallback
        if not isinstance(data["source_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", data["source_sha256"]
        ):
            return fallback
        return {key: data[key] for key in ("build_id", "source_sha256")}
    except (OSError, ValueError, TypeError):
        return fallback


def version_label() -> str:
    return f"Termana {application_version()} ({packaged_build()['build_id']})"


def support_details() -> str:
    """Allowlist useful fields; exclude host/user names, paths, env and SSH data."""
    from PySide6.QtCore import qVersion

    build = packaged_build()
    return "\n".join(
        (
            version_label(),
            f"Source: {build.get('source_sha256', 'working source tree')}",
            f"OS: {platform.system()} {platform.release()}",
            f"Architecture: {platform.machine()}",
            f"Python: {platform.python_version()}",
            f"Qt: {qVersion()}",
        )
    )
