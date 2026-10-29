"""Stage the reviewed Windows component from its existing isolated build archive.

No download, compilation, dependency installation, replacement or PATH lookup.
The runtime pin is the authority, not the archive's filename or manifest claims.
"""

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from termana.rdp.component import _component_spec  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-runtime", required=True, type=Path)
    args = parser.parse_args()
    relative, expected = _component_spec("win32", "AMD64")
    target = PROJECT / relative
    with zipfile.ZipFile(args.windows_runtime) as archive:
        if set(archive.namelist()) != {target.name, "helper-manifest.json"}:
            raise ValueError("Unexpected runtime archive contents")
        item = archive.getinfo(target.name)
        if not 0 < item.file_size <= 100_000_000:
            raise ValueError("Invalid component size")
        data = archive.read(item)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Runtime archive is not the reviewed component")
        raw_manifest = archive.read("helper-manifest.json")
        if len(raw_manifest) > 65536 or json.loads(raw_manifest).get("helper_sha256") != expected:
            raise ValueError("Runtime provenance mismatch")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Create-only or exact idempotence; never repair an unexpected prior file.
    for path, payload in ((target, data), (target.parent.parent / "manifest.json", raw_manifest)):
        if path.is_symlink():
            raise ValueError("Refusing a symlink")
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError("Refusing to replace a different component/provenance")
        else:
            with path.open("xb") as stream:
                stream.write(payload)
    print(json.dumps({"staged": relative.as_posix(), "sha256": expected, "system_changes": False}))


if __name__ == "__main__":
    main()
