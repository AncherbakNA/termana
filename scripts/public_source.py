"""Closed, audited public-source export. Never copy a workspace or Git history."""

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path

BUILD_SCRIPTS = (
    "public_metadata.py",
    "sign_release.py",
    "build_desktop.py",
    "public_source.py",
    "build_ssh1_helper.py",
    "patch_ssh1_frontend.py",
    "ssh1_frontend.h",
    "build_rdp_core.py",
    "build_rdp_crypto.py",
    "rdp_build_job.py",
    "prepare_rdp_packaging.py",
)
NATIVE_FILES = (
    "CMakeLists.txt",
    "client.c",
    "session_channels.c",
    "session_channels.h",
    "platform.c",
    "platform.h",
    "windows_entry.c",
    "spike.c",
)
OVERRIDES = {
    "CHANGELOG.md": "publishing/CHANGELOG.md",
    "README.md": "publishing/README.md",
    "BUILD.md": "publishing/BUILD.md",
    "native/rdp_helper/README.md": "publishing/RDP-BUILD.md",
}
# Fail closed on known private material; never print matching bytes. This is an
# additional gate, NOT proof that arbitrary secrets can always be recognized.
PRIVATE_MARKERS = (
    rb"github_pat_[A-Za-z0-9_]+",
    rb"gh[pousr]_[A-Za-z0-9]{20,}",
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/]{24,}",
    rb"\b10[.](?:[0-9]{1,3}[.]){2}[0-9]{1,3}\b",
    rb"/Users/[A-Za-z0-9._-]+/",
    rb"C:\\Users\\[^\\\r\n\"]+\\",
)


def _read(root, relative):
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Public source must not contain symlinked inputs")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8 * 1024**2:
        raise ValueError("Unsafe or oversized public source input")
    data = path.read_bytes()
    after = path.stat()

    def stamp(p):
        return (p.st_dev, p.st_ino, p.st_size, p.st_mtime_ns, p.st_ctime_ns, p.st_mode, p.st_nlink)

    if len(data) != info.st_size or stamp(after) != stamp(info):
        raise ValueError("Public source changed while reading")
    return data


def audit_payload(name, data):
    if data.startswith((b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"PK\x03\x04")):
        raise ValueError(f"Executable/archive refused in public source: {name}")
    if any(re.search(pattern, data, re.IGNORECASE) for pattern in PRIVATE_MARKERS):
        raise ValueError(f"Private marker refused in public source: {name}")
    data.decode("utf-8")  # Source-only, never silently ingest a binary or secret blob.


def source_payloads(root):
    """Return public path -> exact bytes, including public documentation overrides."""
    root = root.absolute()
    names = {"pyproject.toml", "LICENSE", "src/termana/legacy_helpers/PuTTY-LICENCE.txt"}
    names.update(
        p.relative_to(root).as_posix()
        for p in (root / "src").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    names.update("scripts/" + name for name in BUILD_SCRIPTS)
    names.update("native/rdp_helper/" + name for name in NATIVE_FILES)
    names.update(
        p.relative_to(root).as_posix()
        for p in (root / "packaging").rglob("*")
        if p.is_file() and p.suffix in {".py", ".spec", ".txt"}
    )
    names.update(OVERRIDES)
    # Keep overrides themselves so an exported checkout can run the same builder.
    names.update(OVERRIDES.values())
    result = {}
    for name in sorted(names):
        data = _read(root, OVERRIDES.get(name, name))
        audit_payload(name, data)
        result[name] = data
    return result


def manifest(payloads):
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payloads.items())}


def export_source(root, destination):
    payloads = source_payloads(root)
    destination = destination.absolute()
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError("Public export destination must not contain symlinks")
    destination.mkdir(mode=0o700, exist_ok=False)
    for name, data in payloads.items():
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    return manifest(payloads)


def write_source_archive(root, destination):
    payloads = source_payloads(root)
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            info = zipfile.ZipInfo(name, (2026, 10, 29, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return manifest(payloads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = export_source(Path(__file__).resolve().parents[1], args.output)
    print(
        json.dumps(
            {
                "files": len(records),
                "source_sha256": hashlib.sha256(
                    json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
