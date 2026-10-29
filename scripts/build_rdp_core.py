"""Build reviewed core helpers against the new, manifest-verified private crypto.

Source-only components. No package manager, install target, or QA app packaging.
"""
import argparse
import json
import os
import platform
import sys
import tarfile
from pathlib import Path

from build_rdp_crypto import SOURCE_SHA256 as CRYPTO_SHA256
from build_rdp_crypto import digest, no_symlinks, run

FREERDP_SHA256 = "e95a505c0057e4d06a3b120477c421028769deba4db9c639ef3465d050f0955c"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "archive", "native", "crypto-root", "cmake", "compiler"):
        parser.add_argument("--"+name, required=True, type=Path)
    parser.add_argument("--make", default=Path("/usr/bin/make"), type=Path)
    parser.add_argument("--target", choices=("native", "windows-x64"), default="native")
    parser.add_argument("--clipboard-qa-diagnostics", action="store_true")
    parser.add_argument("--internal-tls-probe", action="store_true")
    args = parser.parse_args()
    cross = args.target=="windows-x64"
    if (sys.platform, platform.machine()) not in (("darwin", "arm64"), ("linux", "x86_64")):
        parser.error("POSIX native build targets only; Windows needs its owned-job recipe")
    if cross and (sys.platform, platform.machine())!=("linux", "x86_64"):
        parser.error("Use the approved Linux x64 Windows cross-builder")
    root, archive, native, crypto = (p.absolute() for p in
        (args.root, args.archive, args.native, args.crypto_root))
    for path in (root, archive, native, crypto):
        no_symlinks(path)
    if root.exists() or not root.parent.is_dir() or "rdp" not in root.parent.name.lower():
        parser.error("A fresh child of the owned RDP root is required")
    if digest(archive) != FREERDP_SHA256:
        parser.error("FreeRDP release checksum mismatch")
    manifest = json.loads((crypto.parent / "manifest.json").read_text())
    target_platform, target_arch = ("win32", "AMD64") if cross else (sys.platform, platform.machine())
    if (manifest.get("version") != "3.5.9" or manifest.get("source_sha256") != CRYPTO_SHA256
            or manifest.get("platform") != target_platform or manifest.get("architecture") != target_arch):
        parser.error("Wrong crypto provenance or native target")
    for name in ("lib/libssl.a", "lib/libcrypto.a"):
        if digest(crypto / name) != manifest["artifacts"]["prefix/"+name]:
            parser.error("Crypto build artifact changed")
    tools = [p.resolve(strict=True) for p in (args.cmake, args.compiler, args.make)]
    if any(not p.is_file() or not os.access(p, os.X_OK) for p in tools):
        parser.error("Explicit existing executable build tools are required")
    cmake, compiler, make = tools
    if cross:
        # Keep the reviewed target-driver basename (the vendor wrapper chooses
        # its Windows sysroot from argv[0]); do not resolve it to generic clang.
        compiler = args.compiler.absolute()
        tools[1] = compiler
        if compiler.name!="x86_64-w64-mingw32-clang":
            parser.error("Explicit x64 Windows cross-compiler required")
    root.mkdir(mode=0o700)
    with tarfile.open(archive) as package:
        members = package.getmembers()
        if len(members)>50000 or sum(m.size for m in members)>500*1024*1024:
            raise ValueError("FreeRDP archive exceeds its extraction budget")
        if any((not m.isdir() and not m.isfile()) or
               (m.name!="freerdp-3.32.2" and not m.name.startswith("freerdp-3.32.2/")) for m in members):
            raise ValueError("Unexpected FreeRDP source member")
        package.extractall(root, filter="data")
    # Preserve the actual user-home identity. FreeRDP allocates settings before
    # our explicit per-attempt ConfigPath override; no HOME makes that allocation
    # fail even for an offline check. Never redirect or modify the user's home.
    environment = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG", "LC_CTYPE") if key in os.environ}
    environment.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", OPENSSL_CONF="/dev/null",
                       SOURCE_DATE_EPOCH="1792972800")
    if cross:
        environment["PATH"] = str(compiler.parent)+os.pathsep+environment["PATH"]
    configure = [str(cmake), "-S", str(native), "-B", str(root/"compile"), "-G", "Unix Makefiles",
        "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_MAKE_PROGRAM={make}",
        f"-DTERMANA_FREERDP_SOURCE={root/'freerdp-3.32.2'}", f"-DOPENSSL_ROOT_DIR={crypto}",
        f"-DOPENSSL_INCLUDE_DIR={crypto/'include'}", f"-DOPENSSL_SSL_LIBRARY={crypto/'lib/libssl.a'}",
        f"-DOPENSSL_CRYPTO_LIBRARY={crypto/'lib/libcrypto.a'}", f"-DCMAKE_C_COMPILER={compiler}",
        "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF", "-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF"]
    if args.clipboard_qa_diagnostics:
        configure.append("-DTERMANA_CLIPBOARD_QA_DIAGNOSTICS=ON")
    if args.internal_tls_probe:
        if not (native / "tls_probe.c").is_file():
            parser.error("Internal TLS probe source is not included in public exports")
        configure.append("-DTERMANA_INTERNAL_TLS_PROBE=ON")
    if sys.platform == "darwin":
        environment["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
        configure += ["-DCMAKE_OSX_ARCHITECTURES=arm64", "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0"]
    if cross:
        configure += ["-DCMAKE_SYSTEM_NAME=Windows", "-DCMAKE_SYSTEM_PROCESSOR=AMD64",
            f"-DCMAKE_CXX_COMPILER={compiler.parent/'x86_64-w64-mingw32-clang++'}",
            f"-DCMAKE_RC_COMPILER={compiler.parent/'llvm-windres'}"]
    run(configure, root, "configure", environment, 180)
    targets = ["termana-rdp-client", "termana-rdp-spike"]
    if not cross and args.internal_tls_probe:
        targets.append("termana-rdp-tls-probe")
    run([str(cmake), "--build", str(root/"compile"), "--parallel", "4", "--target", *targets],
        root, "compile", environment, 900)
    client = root/("compile/termana-rdp-client.exe" if cross else "compile/termana-rdp-client")
    for area in (() if cross else ("settings", "channels", "registration", "paint", "keymap", "platform")):
        run([str(client), f"--{area}-self-test"], root, "self-test-"+area, environment, 15)
        row = json.loads((root/f"self-test-{area}.log").read_text().strip())
        key = "paint_coalesced" if area == "paint" else area
        if row.get(key) is not True or row.get("connect") is not False:
            raise ValueError("Unexpected offline component self-test result")
    if not cross:
        run([str(client), "--crypto-self-test"], root, "self-test-crypto", environment, 15)
        if json.loads((root/"self-test-crypto.log").read_text().strip()) != {
                "crypto": "3.5.9", "sha256": True, "default_provider_only": True, "connect": False}:
            raise ValueError("Runtime crypto provenance/provider smoke check failed")
    with (root/"manifest.json").open("x") as output:
        json.dump(dict(kind="isolated-core-rdp", engine="3.32.2", crypto="3.5.9",
            platform=target_platform, architecture=target_arch, host_platform=sys.platform,
            source_sha256=FREERDP_SHA256, offline_checks="deferred-native-Windows" if cross else "passed",
            crypto_manifest_sha256=digest(crypto.parent/"manifest.json"),
            crypto_artifacts=manifest["artifacts"],
            native_source_sha256={p.name: digest(p) for p in native.iterdir() if p.is_file()},
            helper_sha256=digest(client), tools={str(p): digest(p) for p in tools},
            system_packages=False, python_dependencies=False, qa_applications=False,
            native_acceptance=False,
            clipboard_qa_diagnostics=args.clipboard_qa_diagnostics), output, indent=2)
    print(f"Core helper build and offline tests verified: {root/'manifest.json'}", flush=True)


if __name__ == "__main__":
    main()
