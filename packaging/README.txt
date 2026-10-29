Termana local desktop builds (20 October 2026)

Build natively: .app on macOS, .exe on Windows. No cross-compilation or production
signing/notarization is implied. The current artifacts are unsigned QA builds.
Source launch with python -m termana remains supported.

Use a fresh build environment, not a user's normal installation:

  python -m venv dist/build-env
  dist/build-env/bin/python -m pip install . -r packaging/requirements.txt
  dist/build-env/bin/python scripts/build_desktop.py --output dist/desktop-new

On Windows use dist\build-env\Scripts\python.exe for the last two commands.
--output must be a new directory. Existing artifacts/logs are never overwritten.
Build tools are separate from application runtime dependencies. Review the saved
package versions, source manifest and stage logs in build.json. Repeating a recipe
is supported; bit-for-bit deterministic builds are not claimed.

The helper bootloader supports only fixed Termana roles. It is not python -c/-m.
It shares the desktop bundle's Python libraries instead of unpacking a new runtime
for every terminal. Windows has one windowed single-file exe. The macOS app is a
directory bundle; distribute it with a symlink-preserving archive tool (ditto).
Neither desktop artifact needs a separately installed Python. System OpenSSH is
still an OS capability; its absence is not silently replaced by another engine.

Explicit checks (new report paths only):
  Termana --check-runtime NEW_REPORT.json
  Termana --check-transports NEW_TRANSPORT_REPORT.json

On macOS the executable is Termana.app/Contents/MacOS/Termana. The first check opens
an isolated temporary window and verifies native PTY/ConPTY plus trusted ssh -V.
The second uses disposable loopback SSH/SFTP peers, strict host-key verification,
synthetic credentials and exact binary data. It never connects to a user's host,
loads their connection stores or accesses keychain secrets. Session0 checks on
Windows are not interactive desktop or physical keyboard acceptance.

LICENSE, selected dependency license metadata and the corresponding Termana source
archive are embedded. Public release still needs signing/notarization, a complete
third-party-notice audit, architecture/platform acceptance and delivery decisions.
Do not disable Gatekeeper, SmartScreen or certificate verification globally.
