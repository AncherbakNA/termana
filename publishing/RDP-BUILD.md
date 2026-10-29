# Termana RDP helper

The helper embeds FreeRDP 3.32.2 and OpenSSL 3.5.9 in a windowless process.
Termana owns the interface, credential consent, trust decisions and display/input
integration. It does not implement the RDP protocol itself.

Enabled virtual channels are text clipboard and dynamic display control only.
No drive, device, printer, smart-card or audio redirection is included.

## Isolated build

Obtain the official FreeRDP 3.32.2 and OpenSSL 3.5.9 release source archives.
The exact SHA-256 values are pinned in `scripts/build_rdp_core.py` and
`scripts/build_rdp_crypto.py`. Use existing explicit CMake/compiler/make/perl paths.
Build into new private children of a task-specific directory containing `rdp`
in its name; do not install libraries system-wide or replace an existing build.

```sh
python scripts/build_rdp_crypto.py --root /absolute/rdp-build/crypto --archive /absolute/openssl-3.5.9.tar.gz --compiler /absolute/path/to/compiler
python scripts/build_rdp_core.py --root /absolute/rdp-build/helper --archive /absolute/freerdp-3.32.2.tar.gz --native /absolute/termana/native/rdp_helper --crypto-root /absolute/rdp-build/crypto/prefix --cmake /absolute/path/to/cmake --compiler /absolute/path/to/compiler
```

macOS ARM64 and Linux x86-64 have native recipes. The explicit `windows-x64`
target uses a Linux x86-64 cross-builder with LLVM MinGW; its native Windows
runtime tests must be performed separately. No Windows acceptance is inferred
from successful cross-compilation. Toolchain choices and pins are reviewed, not
discovered from PATH.

Default builds include the client and offline spike target. Internal diagnostic
TLS probes are optional and are not part of public source or desktop packages.
The helper has offline settings/channels/registration/paint/keymap/platform/crypto
self-tests; those checks do not contact a server or replace real interoperability
and desktop input testing.

The desktop packager copies reviewed helper bytes unchanged. On macOS they are
added after PyInstaller processes nested code, so pinned signatures/hashes survive.
See `packaging/licenses` for FreeRDP/OpenSSL license texts and notices.
