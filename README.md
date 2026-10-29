# Termana

A desktop workspace for SSH, SFTP, Telnet and RDP, with local terminals and
saved connections in one window.

[Download releases](https://github.com/AncherbakNA/termana/releases)
· [Source code](https://github.com/AncherbakNA/termana)
· [GPL 3 license](LICENSE)

## Current status

Termana is in active development. Versions `0.x.y` are early releases, not a
production-readiness guarantee. The first public release is planned as `v0.1.0`.
GitHub synchronization and automatic update installation are not available yet.

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

Telnet and optional SSH1 are legacy protocols. Use them only on trusted networks
when a supported encrypted protocol is unavailable. SSH1 is never an automatic
fallback from SSH2.

## Downloads and installation

Get application packages only from this repository's
[Releases page](https://github.com/AncherbakNA/termana/releases). Choose the package
for your operating system and processor; release notes identify available builds.

- **macOS:** extract the archive and move `Termana.app` into Applications.
- **Windows:** place the executable in a directory you control and start it.
- **Linux:** launch from source for now; no Linux desktop package is promised.

Native signing and notarization status is stated for each release. Do not assume
that a manifest signature is an Apple or Microsoft code-signing certificate.
Do not disable operating-system security protections to run an unverified copy.

## Updating

For the current early versions, download a newer complete package from Releases,
quit Termana normally, replace the application and reopen it. Back up configuration
before upgrading. Active sessions do not survive quitting the application.
Application files and personal data are stored separately; do not delete the
data directory when replacing the application.

Signed update metadata and an update-checking core are under development.
Automatic installation is not a released feature yet.

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
