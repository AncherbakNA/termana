# Termana releases

## 0.1.2

- Quiet update checks after startup and every six hours in packaged clients,
  with a saved opt-out. Download and restart/install remain explicit actions.
- HTTPS uses the operating system trust store; a developer's external CA file
  is no longer required for update checks or GitHub sign-in.
- Optional readable JSON sync alongside the encrypted default, with an explicit
  warning about repository access and persistent plaintext in Git history.
- GitHub sign-in has a manual browser link; recovery-key editing works again
  after closing and reopening the sync dialog.
- Windows data-folder ownership no longer blocks the application's own update
  metadata checks. Directory ownership and no-reparse protections remain intact.
- Sync confirmations keep their text and buttons separate at larger fonts on
  macOS and Windows, with Cancel as the safe default.

Mac ARM64 and Windows x86-64 packages are provided; Linux remains source-only.
Personal sync uses explicit Sync now, not periodic background upload. Native
helper versions and trust pins are unchanged. OS signing remains as documented.
If an older client's updater cannot complete the first upgrade, download the
complete package manually, quit normally and replace only the application.
Keep the existing data directory; do not disable TLS or system security checks.

## 0.1.1

- Signed package download and explicit restart/install for macOS ARM64 and
  Windows x86-64, with configuration backup and failed-start rollback.
- Encrypted personal GitHub connection/folder/color synchronization, Device Flow
  login, native credential storage, recovery and explicit conflict review.
- Windows atomic configuration publication and sync backup under application
  ownership corrected without changing directory permissions.
- New-version readiness refuses unsupported configuration instead of accepting
  a default/read-only fallback as a successful update.
- Recovery preserves device identities and merge history when vault data is lost.
- Sync dialog buttons and fields fit larger fonts without overlapping.

Upgrade from0.1.0 manually once; subsequent supported package updates can use
Help → Check for updates. Sync is explicit Sync now, not a background scheduler.
Native versions/pins are unchanged. macOS is ad-hoc sealed/not notarized;
Windows is not Authenticode-signed. Linux remains source-only in this release.

Downloads foreground the two application packages. Signed manifest/signature
are the only additional uploaded assets; matching own source is in the release
tag and the unchanged native source archive is linked from BUILD.md/release notes.

## 0.1.0

First public development release. macOS ARM64 and Windows x86-64 application
packages accompany the source code. Version 0.x is not a production-readiness
guarantee; keep configuration backups before upgrading.

- SSH/SFTP, Telnet, local terminals and embedded RDP in one workspace.
- Saved connections, folders, favorites and per-connection colors.
- Terminal tabs, split panes, command snippets, search and session output logs.
- Local shell and starting-directory preferences, word-navigation shortcuts
  and configurable Backspace behavior.
- RDP keyboard/mouse input, dynamic display, administrative sessions and
  separately enabled text clipboard directions.
- Light/dark themes and configurable fonts and toolbar buttons.
- Explicit signed update checks under Help, using a pinned publisher key and
  persistent rollback protection for accepted metadata.

### Updating

Download the complete package for your OS/processor from this repository's
Releases page. Quit Termana normally before replacing the application. Keep
`~/.termana`; do not overwrite it with another user's configuration.
Sessions are not preserved when quitting. Existing data is backed up before
supported schema upgrades; older versions can refuse newer schemas.

Automatic package installation and GitHub session synchronization are not
available in this release. No background update checks run without requesting one.
The signed update manifest expires after 90 days; a newer immutable release must
refresh the feed before then. Expired metadata is rejected, not treated as current.

### Platform and security notes

macOS application sealing is ad-hoc, not Developer ID signing or notarization.
The Windows executable is not Authenticode-signed. An Ed25519 update signature
does not replace operating-system signing. Do not disable OS security protections.
Linux can run from source; this release does not promise a Linux desktop package.
Telnet and optional SSH1 are legacy protocols for explicitly trusted networks.

Passwords use native system credential stores. Connection settings, private keys,
host trust, logs and publisher private keys are not part of the public release.
