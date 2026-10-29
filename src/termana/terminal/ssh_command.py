"""Build safe system OpenSSH commands from validated connection values."""

import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from termana.models.ssh_algorithms import DEFAULT_SSH_ALGORITHMS, SshAlgorithms
from termana.models.ssh_cipher import validate_ssh_cipher
from termana.models.ssh_identity import (
    SshAuthenticationMode,
    SshIdentityOptions,
    ssh_authentication_arguments,
)
from termana.models.ssh_jump import SshJumpMode, SshJumpOptions, validate_ssh_route_host
from termana.models.ssh_options import SshConnectionOptions
from termana.models.ssh_tunnels import SshTunnelOptions
from termana.terminal.command import ShellCommand
from termana.terminal.ssh_tunnel_arguments import ssh_tunnel_arguments
from termana.terminal.windows_system_paths import native_windows_system_directory

SSH_PROGRAM: Final = "ssh"
SSH_CIPHER_ISOLATION_ARGUMENTS: Final = (
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPersist=no",
    "-S",
    "none",
)
SSH_CONFIGURED_FORWARDS_NOTICE: Final = (
    "These tunnels add to the forwards in your OpenSSH configuration; they do not replace them. "
    "They may also re-enable configured local, remote, dynamic, stream-local, or device tunnels "
    "disabled by ClearAllForwardings. Configured listeners may be reachable from other computers. "
    "Only the local rules created by Termana are restricted to 127.0.0.1. "
    "Continue only if you trust the forwarding settings in your SSH configuration."
)


class SshTunnelConsentRequiredError(ValueError):
    """An explicit launch decision is required before adding forwards to SSH config."""


def resolve_system_ssh_program(
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
    candidates: Sequence[Path] | None = None,
) -> str | None:
    """Return a trusted absolute OpenSSH client path or disable automation."""
    return _resolve_system_openssh_program(
        "ssh", platform_name=platform_name, environment=environment, candidates=candidates
    )


def resolve_system_ssh_keygen_program(
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
    candidates: Sequence[Path] | None = None,
) -> str | None:
    """Use the same system trust policy for the helper that can edit known_hosts."""
    return _resolve_system_openssh_program(
        "ssh-keygen", platform_name=platform_name, environment=environment, candidates=candidates
    )


def _resolve_system_openssh_program(
    name: str,
    *,
    platform_name: str | None,
    environment: Mapping[str, str] | None,
    candidates: Sequence[Path] | None,
) -> str | None:
    """Resolve a fixed OpenSSH tool without consulting the working directory or PATH."""
    selected_platform = sys.platform if platform_name is None else platform_name
    require_system_ownership = candidates is None and selected_platform != "win32"
    if candidates is None:
        if selected_platform == "win32":
            # SystemRoot is inherited input, not proof of an OS-owned location.
            # Keep the environment argument for API compatibility, never discovery.
            system_directory = native_windows_system_directory()
            if system_directory is None:
                return None
            candidates = (Path(system_directory) / "OpenSSH" / f"{name}.exe",)
        else:
            candidates = (Path("/usr/bin") / name, Path("/bin") / name)

    for candidate in candidates:
        if not candidate.is_absolute():
            # An invalid native directory result (or an injected test candidate)
            # must not silently turn into an executable in the working directory.
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        try:
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and os.access(resolved, os.X_OK)
            and (
                not require_system_ownership
                or (metadata.st_uid == 0 and metadata.st_mode & 0o022 == 0)
            )
        ):
            return str(resolved)
    return None


def build_ssh_command(
    *,
    host: str,
    port: int,
    username: str | None,
    program: str = SSH_PROGRAM,
    ssh_cipher: str | None = None,
    ssh_algorithms: SshAlgorithms = DEFAULT_SSH_ALGORITHMS,
    ssh_options: SshConnectionOptions | None = None,
    ssh_identity: SshIdentityOptions | None = None,
    ssh_jump: SshJumpOptions | None = None,
    ssh_tunnels: SshTunnelOptions | None = None,
    configured_forwards_confirmed: bool = False,
    known_hosts: Path | None = None,
    system_config: Path | None = None,
) -> ShellCommand:
    """Return an argv-based SSH command without invoking a command shell.

    Nonempty tunnels require runtime confirmation of the inherited-config notice.
    This decision belongs to the launching caller, never to an imported profile.
    Empty tunnels retain ordinary OpenSSH configuration and need no confirmation.
    """
    if not isinstance(program, str) or not program:
        raise ValueError("SSH program cannot be empty")
    selected_cipher = validate_ssh_cipher(ssh_cipher)
    if ssh_options is not None and type(ssh_options) is not SshConnectionOptions:
        raise ValueError("SSH options must be validated connection options")
    selected_options = SshConnectionOptions() if ssh_options is None else ssh_options
    if ssh_identity is not None and type(ssh_identity) is not SshIdentityOptions:
        raise ValueError("SSH identity must be validated identity options")
    selected_identity = SshIdentityOptions() if ssh_identity is None else ssh_identity
    if ssh_jump is not None and type(ssh_jump) is not SshJumpOptions:
        raise ValueError("SSH jump must be validated jump options")
    selected_jump = SshJumpOptions() if ssh_jump is None else ssh_jump
    if ssh_tunnels is not None and type(ssh_tunnels) is not SshTunnelOptions:
        raise ValueError("SSH tunnels must be validated SshTunnelOptions")
    selected_tunnels = SshTunnelOptions() if ssh_tunnels is None else ssh_tunnels
    if type(configured_forwards_confirmed) is not bool:
        raise ValueError("SSH configured-forward confirmation must be a boolean")
    if not selected_tunnels.is_default and not configured_forwards_confirmed:
        raise SshTunnelConsentRequiredError(SSH_CONFIGURED_FORWARDS_NOTICE)
    if selected_jump.mode is SshJumpMode.JUMP:
        # OpenSSH internally expands the destination into its generated proxy
        # command. An argv boundary alone does not protect that later shell use.
        validate_ssh_route_host(host.strip())
    arguments = ["-p", str(port)]
    if type(ssh_algorithms) is not SshAlgorithms:
        raise ValueError("SSH algorithms must be SshAlgorithms")
    if ssh_algorithms.protocol == "1":
        raise ValueError("SSH1 requires the separate reviewed legacy component")
    if ssh_algorithms.protocol == "2":
        arguments.append("-2")
    for keyword, names in (("KexAlgorithms", ssh_algorithms.kex),
                           ("HostKeyAlgorithms", ssh_algorithms.host_keys)):
        if names:
            arguments.extend(("-o", keyword + "=" + ",".join(names)))
    if username is not None:
        arguments.extend(("-l", username.strip()))
    if system_config is not None:
        if (not isinstance(system_config, Path) or not system_config.is_absolute()
                or re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', system_config.name) is None):
            raise ValueError('System SSH config requires an absolute path and a shell-safe basename')
        # Native -J repeats -F through its generated proxy command without shell
        # quoting. Keep its token ASCII-safe and give every child this exact cwd.
        arguments.extend(('-F', system_config.name))
    if system_config is not None:
        if known_hosts != system_config.parent.parent / "known_hosts":
            raise ValueError("Managed SSH config requires its own adjacent trust file")
        # The generated snapshot uses ../known_hosts from the pinned child cwd.
        # Avoid absolute percent expansion, absent in older inbox OpenSSH.
        # Do not override that policy through a command-line trust option.
    elif known_hosts is not None:
        arguments.extend(ssh_known_hosts_arguments(known_hosts))
    if selected_cipher is not None:
        arguments.extend(("-c", selected_cipher))
    for option_name, option_value in (
        ("ConnectTimeout", selected_options.connect_timeout),
        ("ServerAliveInterval", selected_options.server_alive_interval),
        ("ServerAliveCountMax", selected_options.server_alive_count_max),
    ):
        if option_value is not None:
            arguments.extend(("-o", f"{option_name}={option_value}"))
    key_enabled = selected_identity.authentication is not SshAuthenticationMode.PASSWORD_ONLY
    if key_enabled and selected_identity.identity_file is not None:
        arguments.extend(("-i", selected_identity.identity_file))
    if key_enabled and selected_identity.identities_only is not None:
        value = "yes" if selected_identity.identities_only else "no"
        arguments.extend(("-o", f"IdentitiesOnly={value}"))
    if key_enabled and selected_identity.agent_disabled:
        arguments.extend(("-o", "IdentityAgent=none"))
    arguments.extend(ssh_authentication_arguments(selected_identity.authentication))
    if selected_jump.mode is SshJumpMode.JUMP:
        arguments.extend(("-J", selected_jump.proxy_jump_value))
    elif selected_jump.mode is SshJumpMode.DIRECT:
        arguments.extend(("-o", "ProxyJump=none", "-o", "ProxyCommand=none"))
    arguments.extend(ssh_tunnel_arguments(selected_tunnels))
    if (
        selected_cipher is not None
        or not selected_options.is_default
        or not selected_identity.is_default
        or not selected_jump.is_default
        or not selected_tunnels.is_default
    ):
        # A shared connection has already negotiated its cipher and owns its
        # connection, identity, route and tunnels. Overrides need a fresh destination
        # connection; a native jump child still uses its own OpenSSH configuration.
        arguments.extend(SSH_CIPHER_ISOLATION_ARGUMENTS)
    arguments.extend(("--", host.strip()))
    return ShellCommand(program=program, arguments=tuple(arguments),
                        working_directory=None if system_config is None else str(system_config.parent))


def ssh_known_hosts_arguments(path: Path) -> tuple[str, str]:
    """Quote OpenSSH's config grammar too, not only the subprocess argv boundary."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("SSH trust path must be absolute")
    value = path.as_posix()
    if any(c in value for c in ('\0', '\r', '\n', '${')):
        raise ValueError("SSH trust path contains unsupported expansion/control characters")
    value = value.replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')
    return '-o', f'UserKnownHostsFile="{value}"'
