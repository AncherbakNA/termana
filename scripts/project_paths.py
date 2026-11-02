"""Checkout-local outputs, with exact opt-in aliases for the personal workspace."""

import os
from pathlib import Path

PROJECT = Path(__file__).absolute().parent.parent
ROOT = PROJECT.parent
SECRETS = ROOT / "secrets"
ARTIFACTS = ROOT / "artifacts"


def _artifact_directory(name):
    path = PROJECT / name
    # Do not normalize the link text or hide any other link from path guards.
    if path.is_symlink() and os.readlink(path) == f"../artifacts/{name}":
        return ARTIFACTS / name
    return path


DIST = _artifact_directory("dist")
BUILD = _artifact_directory("build")
