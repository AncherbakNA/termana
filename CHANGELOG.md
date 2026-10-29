# Termana releases

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
