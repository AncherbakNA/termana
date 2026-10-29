"""Public release trust anchor. Never import publisher private keys here."""

import base64
import platform
import sys

from termana.updates.manifest import ReleaseRepository, UpdateError

REPOSITORY = ReleaseRepository("AncherbakNA", "termana")
PUBLIC_KEY = base64.b64decode("9UQtjyKpAYZs0aMQq0srt3dMHGAx472rhXy9NgvGMlA=")
PUBLIC_KEY_SHA256 = "12507049dfeeecf94bc9be8833b0767ec6e9bcebed290aad8f58614c2d445e21"


def native_target():
    try:
        system = {"darwin": "macos", "win32": "windows", "linux": "linux"}[sys.platform]
        architecture = {
            "arm64": "arm64",
            "aarch64": "arm64",
            "amd64": "x86_64",
            "x86_64": "x86_64",
        }[platform.machine().lower()]
    except KeyError:
        raise UpdateError("No update package is supported for this platform") from None
    return system, architecture
