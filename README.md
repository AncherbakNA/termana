# Termana

A desktop workspace for SSH, SFTP, Telnet and RDP, with local terminals and
saved connections in one window.

[Download releases](https://github.com/AncherbakNA/termana/releases)
· [Source code](https://github.com/AncherbakNA/termana)
· [GPL 3 license](LICENSE)

## Current status

Termana is in active development. Versions `0.x.y` are early releases, not a
production-readiness guarantee. Version `v0.1.1` adds encrypted GitHub session
sync and explicit download/restart installation for supported desktop packages.

## Features

- SSH and SFTP using an embedded engine, with an explicit system OpenSSH mode.
- Saved SSH, Telnet and RDP connections, folders, favorites and connection colors.
- Terminal tabs and split panes, including moving tabs between panes.
- Local terminals with a choice of shell and starting directory.
- Remote file browsing, transfers and text editing over SFTP.
- Embedded RDP desktop with keyboard and mouse input, display resizing,
  administrative sessions and separately enabled clipboard directions.
- Command snippets, terminal search, output saving and session logging.
- Light and dark themes, configurable terminal fonts and toolbar buttons.
- Encrypted personal GitHub sync, recovery keys and explicit conflict review.
- Signed application downloads, user-confirmed restart and failed-start rollback.

Telnet and optional SSH1 are legacy protocols. Use them only on trusted networks
when a supported encrypted protocol is unavailable. SSH1 is never an automatic
fallback from SSH2.

## Downloads and installation

Get application packages only from this repository's
[Releases page](https://github.com/AncherbakNA/termana/releases). Choose the package
for your operating system and processor; release notes identify available builds.

- **macOS:** extract the archive and place `Termana.app` in your own `~/Applications`.
- **Windows:** place the executable in a directory you control and start it.
- **Linux:** launch from source for now; no Linux desktop package is promised.

Native signing and notarization status is stated for each release. Do not assume
that a manifest signature is an Apple or Microsoft code-signing certificate.
Do not disable operating-system security protections to run an unverified copy.
Native RDP and SSH1 require a location other users cannot write to; the system
`/Applications` directory can be refused if it is group-writable. Use your own
application directory rather than changing system permissions.

## Updating

For the first upgrade from `v0.1.0`, download the newer complete package from
Releases, quit normally, replace the application and reopen it. Back up
configuration before upgrading. Active sessions do not survive quitting.
Application files and personal data are stored separately; do not delete the
data directory when replacing the application.

From `v0.1.1`, use **Help → Check for updates → Download update → Restart and
install** on supported frozen macOS/Windows packages. The publisher signature and
package checksum are verified; the previous app and configuration backup are
retained for failed-start rollback. Updates are checked only when requested.
Source launches and Linux installations update manually from source.

## Personal GitHub sync

Create your own private sync repository and install
[Termana Sync](https://github.com/apps/termana-sync) only on that repository.
Use **Sessions → GitHub session sync** to sign in through GitHub's device page.
On the first computer, connect and save the recovery key privately before syncing.
On another computer, select the same repository and enter that recovery key.
Choose **Sync now** to exchange edits; conflicts require an explicit choice.
This is not periodic background synchronization. Expired authorization requires
signing in again; a missing vault entry can be restored with the backup key.

Connections, folders and colors are encrypted before upload. Passwords, private
keys, credential bindings, host trust, logs and live sessions remain local.
Losing every copy of the recovery key cannot be repaired by GitHub login alone.

## Your data

Settings, saved connections, app-owned host trust and logs are kept in `~/.termana`
(`%USERPROFILE%\.termana` on Windows). Saved passwords use the supported native
credential store: macOS Keychain, Windows Credential Manager or Linux Secret Service.
Passwords, private keys, host trust and logs are not uploaded to this public repository.

Do not publish connection files or logs when asking for help. They can contain
private hostnames, addresses, commands and terminal output.

## Run from source

Use Python 3.12 or later in an isolated virtual environment:

```sh
python -m venv .venv
```

Activate it, then install and run:

```sh
python -m pip install -e .
python -m termana
```

For development tools, install with `python -m pip install -e '.[dev]'`.
RDP and SSH1 additionally require their platform-specific native helpers; a Python
installation alone does not build or install them. Desktop release packages
include the helpers identified in their release notes.

## License

Termana is free software under **GPL-3.0-only**. See [LICENSE](LICENSE).
Third-party components retain their own licenses and notices.
