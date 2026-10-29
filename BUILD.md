# Building Termana

Use Python 3.12 or later in a private, isolated virtual environment. Install the
project with `python -m pip install -e '.[dev]'`, then run `python -m termana`.
No personal settings, credentials, host trust or connection files belong in source.

## Native desktop packages

Build on the target operating system; PyInstaller does not cross-compile the
desktop application. Install `packaging/requirements.txt` into a dedicated build
environment. Stage the reviewed native RDP and SSH1 components first, then run:

```sh
python scripts/build_desktop.py --output /absolute/new/build-directory --build-label release.0.1.0
```

On Windows, use a new absolute Windows path instead. The output includes the
application, its exact source archive and build identity. Existing output paths
are refused. Logs and local absolute paths in build reports are not public assets.

The source exporter uses a closed allowlist and public documentation overrides.
It refuses known private markers, linked inputs and binary files. Review exported
files before publication; pattern matching cannot prove that every secret is absent.

## Native components and corresponding source

RDP uses FreeRDP 3.32.2 and OpenSSL 3.5.9, statically linked into the helper.
See [native build instructions](native/rdp_helper/README.md). SSH1 uses PuTTY 0.85
with Termana's no-platform-storage frontend. Download official source archives,
verify the checksums pinned in build scripts, and build in NEW private directories.
Never substitute a helper from PATH or disable a runtime hash check.

For SSH1, run the build as a module from the source root:

```sh
python -m scripts.build_ssh1_helper --archive /absolute/putty-0.85.tar.gz --output /absolute/new/ssh1-build --cmake /absolute/path/to/cmake --frontend
```

Helper hashes are deliberately pinned in `src/termana/terminal/legacy_ssh.py`
and `src/termana/rdp/component.py`. A rebuilt helper may have a different hash
because of toolchains or signing. Review and test it before explicitly updating
the pin; a rebuild must not silently authorize a new executable. Source release
assets identify the exact upstream archives and helper provenance for each binary.

Public update verification uses a pinned Ed25519 public key. A manifest signature
is not Apple notarization or Microsoft Authenticode signing. Publisher private
keys are separate and are never required to build or run from source.
