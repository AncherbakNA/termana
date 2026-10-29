"""Experimental, isolated Plink helper build; not a Termana desktop QA package.

Requires a native C compiler, CMake and the separately downloaded official 0.85
source archive. Never installs anything, changes user data, invokes Git or uses
PuTTY's platform session/host-key/random-seed storage. Runtime integration remains
gated separately; the output alone does not enable SSH1 in Termana.
"""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path

from scripts.patch_ssh1_frontend import patch_frontend

SOURCE_SHA256 = "13fd4db2936d03b73812a7bcc2a658e4dd29cc776a56c3670a7fc6f1a0ee8af8"


def native_build_options():
    # Match the reviewed Qt runtime's LC_BUILD_VERSION minimum, not the build
    # host's OS26. This is a link target, not an older-macOS acceptance claim.
    return ("-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0",) if platform.system() == "Darwin" else ()


def patch_source(root: Path) -> None:
    """Mechanical build-only substitution, without altering SSH/crypto code."""
    original = "add_sources_from_current_dir(settings\n  storage.c)"
    replacement = "target_sources(settings PRIVATE ${CMAKE_SOURCE_DIR}/stubs/no-storage.c)"
    for name in ("unix", "windows"):
        path = root / name / "CMakeLists.txt"
        text = path.read_text()
        if text.count(original) != 1:
            raise ValueError("The reviewed storage build hook changed")
        path.write_text(text.replace(original, replacement))
    stub = root / "stubs/no-storage.c"
    stub.write_text(
        stub.read_text()
        + "\n#ifdef PUTTY_UNIX_PLATFORM_H\nvoid provide_xrm_string(const char *value, const char *program) {}\n#endif\n"
    )
    # Release tarballs already contain a commit ID. Don't discover or run Git.
    setup = root / "cmake/setup.cmake"
    text = setup.read_text()
    if text.count("include(FindGit)") != 1:
        raise ValueError("The reviewed build-tool hook changed")
    setup.write_text(text.replace("include(FindGit)", "# Termana: no Git discovery"))
    commit = root / "cmake/gitcommit.cmake"
    commit.write_text(
        'file(WRITE "${OUTPUT_FILE}" "#include \\"putty.h\\"\\n'
        'const char commitid[] = \\"putty-0.85-termana-no-storage-v1\\";\\n")\n'
    )


def build(archive: Path, output: Path, cmake: Path, *, frontend: bool = False) -> None:
    if not cmake.is_absolute() or not cmake.is_file():
        raise ValueError("Choose an absolute native CMake executable")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("The official PuTTY 0.85 source checksum does not match")
    # No reuse or destructive cleanup of another build, even after failure.
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if any(
            (m.name != "putty-0.85" and not m.name.startswith("putty-0.85/"))
            or ".." in Path(m.name).parts
            or not (m.isfile() or m.isdir())
            for m in members
        ):
            raise ValueError("Unexpected source archive members")
        source.extractall(output, members=members, filter="data")
    root, compiled = output / "putty-0.85", output / "build"
    patch_source(root)
    if frontend:
        patch_frontend(root)
    subprocess.run(
        [
            str(cmake),
            "-S",
            str(root),
            "-B",
            str(compiled),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DPUTTY_GSSAPI=OFF",
            "-DPUTTY_GTK_VERSION=NONE",
            "-DCMAKE_DISABLE_FIND_PACKAGE_Git=ON",
            *native_build_options(),
        ],
        check=True,
        timeout=120,
    )
    subprocess.run(
        [
            str(cmake),
            "--build",
            str(compiled),
            "--config",
            "Release",
            "--target",
            "plink",
            "--parallel",
            "2",
        ],
        check=True,
        timeout=300,
    )
    name = "plink.exe" if platform.system() == "Windows" else "plink"
    candidates = (compiled / name, compiled / "Release" / name)
    program = next(p for p in candidates if p.is_file())
    version = subprocess.run([str(program), "-V"], capture_output=True, check=True, timeout=5)
    published = output / (
        "termana-ssh1-helper.exe" if name.endswith(".exe") else "termana-ssh1-helper"
    )
    shutil.copy2(program, published)
    if platform.system() != "Windows":
        published.chmod(0o755)
    shutil.copy2(root / "LICENCE", output / "PuTTY-LICENCE.txt")
    record = dict(
        experimental=True,
        enabled_in_termana=False,
        frontend_ipc=1 if frontend else None,
        upstream="PuTTY 0.85",
        source_sha256=SOURCE_SHA256,
        helper_sha256=hashlib.sha256(published.read_bytes()).hexdigest(),
        system=platform.system(),
        machine=platform.machine(),
        storage="upstream stubs/no-storage.c; no platform storage linked into settings",
        version=version.stdout.decode("utf-8", "replace"),
    )
    (output / "build-record.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cmake", type=Path, required=True)
    parser.add_argument("--frontend", action="store_true")
    args = parser.parse_args()
    build(args.archive.resolve(), args.output.absolute(), args.cmake, frontend=args.frontend)
