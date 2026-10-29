"""Isolated approved OpenSSL3.5.9 build. No package manager or system installation."""

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from rdp_build_job import own_windows_build_tree, terminate_windows_build_tree

VERSION = "3.5.9"
SOURCE_URL = "https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz"
SOURCE_SHA256 = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
TARGETS = {("darwin", "arm64"): "darwin64-arm64-cc", ("linux", "x86_64"): "linux-x86_64",
           ("win32", "AMD64"): "mingw64", ("win32", "x86_64"): "mingw64"}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def no_symlinks(path):
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Build inputs/output must be absolute, without symlinks")


def run(command, root, name, environment, timeout, *, cwd=None):
    log = root / f"{name}.log"
    print(f"OpenSSL: {name}", flush=True)
    with log.open("xb") as output:
        process = subprocess.Popen(command, cwd=cwd or root, env=environment,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name=="posix",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "nt":
                output.write(b"\nOwned compiler job exceeded its time limit; terminating tree.\n")
                output.flush()
                terminate_windows_build_tree()
                raise RuntimeError("Windows build tree termination returned unexpectedly") from None
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            raise
    if code:
        raise RuntimeError(f"Owned crypto build failed: {name}, exit {code}; preserve its log")
    return log


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--compiler", required=True, type=Path)
    parser.add_argument("--perl", type=Path, default=Path("/usr/bin/perl"))
    parser.add_argument("--make", type=Path, default=Path("/usr/bin/make"))
    parser.add_argument("--target", choices=("native", "windows-x64"), default="native")
    args = parser.parse_args()
    target = TARGETS.get((sys.platform, platform.machine()))
    cross = args.target=="windows-x64"
    if cross:
        if (sys.platform, platform.machine())!=("linux", "x86_64"):
            parser.error("The reviewed Windows cross recipe requires the approved Linux x64 builder")
        if args.compiler.name!="x86_64-w64-mingw32-clang":
            parser.error("An explicit Windows x64 cross-compiler is required")
        target = "mingw64"
    if target is None:
        parser.error("Only approved native Mac ARM64/Linux x64/Windows x64 targets are accepted")
    root, archive = args.root.absolute(), args.archive.absolute()
    for path in (root, archive):
        no_symlinks(path)
    if root.exists() or not root.parent.is_dir() or "rdp" not in root.parent.name.lower():
        parser.error("Use a new task-specific child of an existing RDP build root")
    if digest(archive) != SOURCE_SHA256:
        parser.error("Approved official OpenSSL source checksum mismatch")
    for tool in (args.compiler, args.perl, args.make):
        if not tool.is_absolute() or not tool.is_file() or not os.access(tool, os.X_OK):
            parser.error("Explicit existing build tools are required")
    root.mkdir(mode=0o700)
    own_windows_build_tree()
    started = time.monotonic()
    source, build, prefix = root / f"openssl-{VERSION}", root / "compile", root / "prefix"
    with tarfile.open(archive) as package:
        members = package.getmembers()
        if len(members) > 60000 or sum(m.size for m in members) > 700*1024*1024:
            raise ValueError("Source archive exceeds the approved extraction budget")
        if any(not m.name.startswith(f"openssl-{VERSION}/") and m.name != f"openssl-{VERSION}"
               for m in members):
            raise ValueError("Unexpected source archive root")
        package.extractall(root, filter="data")
    build.mkdir(mode=0o700)
    environment = {key: os.environ[key] for key in ("TMPDIR", "LANG", "LC_CTYPE") if key in os.environ}
    environment.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", CC=str(args.compiler),
                       OPENSSL_CONF="/dev/null", SOURCE_DATE_EPOCH="1792972800")
    if os.name == "nt":
        for key in ("SystemRoot", "SystemDrive", "TEMP", "TMP", "COMSPEC"):
            if key in os.environ:
                environment[key] = os.environ[key]
        bins = [args.compiler.parent, args.perl.parent, args.make.parent,
                Path(environment["SystemRoot"])/"System32"]
        environment["PATH"] = os.pathsep.join(str(p) for p in bins)
        environment["CC"] = args.compiler.as_posix()
        environment["AR"] = (args.compiler.parent/"llvm-ar.exe").as_posix()
        environment["RANLIB"] = (args.compiler.parent/"llvm-ranlib.exe").as_posix()
        environment["RC"] = (args.compiler.parent/"llvm-windres.exe").as_posix()
        environment["OPENSSL_CONF"] = "NUL"
    if sys.platform == "darwin":
        environment["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
    if cross:
        environment["PATH"] = str(args.compiler.parent)+os.pathsep+environment["PATH"]
        for key, name in (("AR", "llvm-ar"), ("RANLIB", "llvm-ranlib"), ("RC", "llvm-windres")):
            environment[key] = str(args.compiler.parent/name)
    options = ["no-shared", "no-module", "no-legacy", "no-tests", "no-zlib", "no-comp",
               f"--prefix={prefix.as_posix()}", f"--openssldir={(prefix/'config').as_posix()}", "--libdir=lib"]
    if os.name == "posix" and not cross:
        options.append("-fPIC")
    run([str(args.perl), str(source / "Configure"), target, *options], root, "configure",
        environment, 90, cwd=build)
    run([str(args.make), "-j4", "build_sw"], root, "compile", environment, 900, cwd=build)
    # install_sw writes ONLY into this fresh, explicit private prefix; no system path.
    run([str(args.make), "install_sw"], root, "stage-private-prefix", environment, 120, cwd=build)
    executable = prefix / ("bin/openssl.exe" if os.name=="nt" or cross else "bin/openssl")
    if not cross:
        run([str(executable), "version", "-a"], root, "version", environment, 10)
        if not (root / "version.log").read_text().startswith(f"OpenSSL {VERSION} "):
            raise ValueError("Built OpenSSL version differs from the approved version")
        known = root / "sha256-known-input"
        known.write_bytes(b"abc")
        run([str(executable), "dgst", "-sha256", str(known)], root, "known-answer",
            environment, 10)
        if "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" not in (
                root / "known-answer.log").read_text():
            raise ValueError("Built crypto failed the SHA256 known-answer smoke test")
    artifacts = [prefix / name for name in ("lib/libssl.a", "lib/libcrypto.a")]+[executable]
    manifest = dict(kind="isolated-rdp-crypto", version=VERSION, platform="win32" if cross else sys.platform,
        architecture="AMD64" if cross else platform.machine(), host_platform=sys.platform,
        native_verified=not cross, source_url=SOURCE_URL, source_sha256=SOURCE_SHA256,
        configure_target=target, configure_options=options, compiler_sha256=digest(args.compiler),
        artifacts={str(path.relative_to(root)): digest(path) for path in artifacts},
        installed_system_packages=False, changed_python_dependencies=False,
        qa_application_built=False, elapsed_seconds=round(time.monotonic()-started, 2))
    with (root / "manifest.json").open("x") as output:
        output.write(json.dumps(manifest, indent=2)+"\n")
    print(f"OpenSSL isolated build verified: {root / 'manifest.json'}", flush=True)


if __name__ == "__main__":
    main()
