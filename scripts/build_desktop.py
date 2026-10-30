"""Build native desktop artifacts in a new directory; never mutate runtime installs."""

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

# Also usable through runpy from the project's read-only packaging checks.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_source import OVERRIDES, source_payloads, write_source_archive


def source_files(root):
    return sorted({root / OVERRIDES.get(name, name) for name in source_payloads(root)})


def source_manifest(root):
    return {name: hashlib.sha256(data).hexdigest() for name, data in source_payloads(root).items()}


def run_stage(command, root, log):
    process = subprocess.Popen(
        command, cwd=root, stdout=log, stderr=log, start_new_session=os.name != "nt"
    )
    try:
        return process.wait(timeout=600)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=log,
                stderr=log,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        return 124


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New, non-existing build root")
    parser.add_argument(
        "--build-label",
        default="qa",
        help="Non-secret checkpoint label; source digest is always appended",
    )
    options = parser.parse_args(arguments)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", options.build_label):
        parser.error("Invalid build label")
    if sys.platform not in {"darwin", "win32"}:
        parser.error("Build .app on macOS or .exe on Windows; cross-compilation is not supported")
    try:
        build_version = metadata.version("pyinstaller")
    except metadata.PackageNotFoundError:
        parser.error("Install packaging/requirements.txt in the build environment first")
    if build_version != "6.22.3":
        parser.error("Install the pinned packaging requirements in a build environment")
    root = Path(__file__).resolve().parents[1]
    source_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    if metadata.version("termana") != source_version:
        parser.error(
            "The build environment Termana version does not match the selected source. Install that source into the isolated build environment first."
        )
    destination = options.output.resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    records = source_manifest(root)
    from termana.rdp.component import verified_component

    component = verified_component()
    component_digest = hashlib.sha256(component.read_bytes()).hexdigest()
    # Public provenance is generated from the selected verified artifact, never
    # copied wholesale from a manifest containing local compiler/home paths.
    with (destination / "rdp-component.json").open("x", encoding="utf-8") as stream:
        json.dump(
            dict(
                engine="FreeRDP",
                engine_version="3.32.2",
                crypto="OpenSSL",
                crypto_version="3.5.9",
                platform=sys.platform,
                architecture=platform.machine(),
                helper_sha256=component_digest,
            ),
            stream,
        )
    source_identity = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    build_id = f"{options.build_label}.{source_identity[:12]}"
    with (destination / "build-info.json").open("x", encoding="utf-8") as stream:
        json.dump(dict(schema_version=1, build_id=build_id, source_sha256=source_identity), stream)
    if write_source_archive(root, destination / "source.zip") != records:
        raise ValueError("Source changed before archive publication")
    report = dict(
        passed=False,
        platform=sys.platform,
        architecture=platform.machine(),
        python=platform.python_version(),
        packages={
            name: metadata.version(name)
            for name in (
                "pyinstaller",
                "pyinstaller-hooks-contrib",
                "termana",
                "PySide6",
                "asyncssh",
                "cryptography",
                "bcrypt",
                "cffi",
                "keyring",
                "pyte",
                "wcwidth",
            )
        },
        source_sha256=records,
        source_identity=source_identity,
        rdp_component_sha256=component_digest,
        build_id=build_id,
        signed_for_public_distribution=False,
        stages=[],
    )
    try:
        for stage in ("runtime", "termana"):
            command = [
                sys.executable,
                "-m",
                "PyInstaller",
                "--distpath",
                str(destination / "artifacts"),
                "--workpath",
                str(destination / "work"),
                str(root / f"packaging/{stage}.spec"),
            ]
            with (destination / f"{stage}.log").open("xb") as log:
                code = run_stage(command, root, log)
            report["stages"].append(dict(name=stage, exit=code, command=command))
            if code:
                return code
        report["source_unchanged"] = (
            source_manifest(root) == records
            and hashlib.sha256(verified_component().read_bytes()).hexdigest() == component_digest
        )
        if not report["source_unchanged"]:
            return 3
        artifact = (
            destination
            / "artifacts"
            / ("Termana.app" if sys.platform == "darwin" else "Termana.exe")
        )
        report["artifact"] = str(artifact)
        report["passed"] = artifact.exists()
        print(json.dumps({k: v for k, v in report.items() if k != "source_sha256"}))
        return 0 if report["passed"] else 1
    finally:
        with (destination / "build.json").open("x") as stream:
            json.dump(report, stream, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
