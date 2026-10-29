"""Pure, local-only SSH identity selection without reading key material."""

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final

MAX_SSH_IDENTITY_FILE_CHARACTERS: Final = 4096


class SshAuthenticationMode(StrEnum):
    CONFIG = "config"
    KEY_ONLY = "key-only"
    PASSWORD_ONLY = "password-only"


def ssh_authentication_arguments(mode: SshAuthenticationMode) -> tuple[str, ...]:
    """Canonical, cohesive overrides; the default emits nothing."""
    if type(mode) is not SshAuthenticationMode:
        raise ValueError("Invalid SSH authentication mode")
    if mode is SshAuthenticationMode.CONFIG:
        return ()
    key = mode is SshAuthenticationMode.KEY_ONLY
    return (
        "-o",
        f"PreferredAuthentications={'publickey' if key else 'password'}",
        "-o",
        f"PubkeyAuthentication={'yes' if key else 'no'}",
        "-o",
        f"PasswordAuthentication={'no' if key else 'yes'}",
        "-o",
        "KbdInteractiveAuthentication=no",
    )


@dataclass(frozen=True, slots=True)
class SshIdentityOptions:
    """Explicit local overrides; omitted choices retain OpenSSH configuration."""

    identity_file: str | None = None
    identities_only: bool | None = None
    agent_disabled: bool = False
    authentication: SshAuthenticationMode = SshAuthenticationMode.CONFIG

    def __post_init__(self) -> None:
        """Validate literal paths and exact flags without filesystem access or expansion."""
        if type(self.authentication) is not SshAuthenticationMode:
            raise ValueError("SSH authentication must be a validated mode")
        if self.identities_only is not None and type(self.identities_only) is not bool:
            raise ValueError("SSH identities_only must be a bool or None")
        if type(self.agent_disabled) is not bool:
            raise ValueError("SSH agent_disabled must be a bool")
        path = self.identity_file
        if path is None:
            return
        if type(path) is not str:
            raise ValueError("SSH identity file must be a string or None")
        if not path or len(path) > MAX_SSH_IDENTITY_FILE_CHARACTERS:
            raise ValueError("SSH identity file must contain between 1 and 4096 characters")
        if path != path.strip():
            raise ValueError("SSH identity file cannot have surrounding whitespace")
        if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in path):
            raise ValueError("SSH identity file cannot contain control or format characters")
        if "%" in path or "${" in path:
            raise ValueError("SSH identity file cannot contain OpenSSH expansion tokens")
        if not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
            raise ValueError("SSH identity file must be an absolute path")

    @property
    def is_default(self) -> bool:
        """Keep explicit false distinct from an untouched identities-only setting."""
        return (
            self.identity_file is None
            and self.identities_only is None
            and not self.agent_disabled
            and self.authentication is SshAuthenticationMode.CONFIG
        )
