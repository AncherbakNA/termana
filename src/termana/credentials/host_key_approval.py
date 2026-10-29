"""Bind a one-attempt host-key decision to trusted OpenSSH AskPass metadata."""

import re
from dataclasses import dataclass

from termana.models.ssh_credential import SshEndpoint
from termana.terminal.known_hosts import known_hosts_target

_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")
_KEY_LINE = re.compile(
    r"^(ED25519|ECDSA|RSA) key fingerprint is:? (SHA256:[A-Za-z0-9+/]{43})\.?$",
    re.MULTILINE,
)
_QUESTION_SUFFIXES = (
    "Are you sure you want to continue connecting (yes/no/[fingerprint])?",
    "Are you sure you want to continue connecting (yes/no)?",
)


@dataclass(frozen=True, slots=True)
class SshHostKeyApproval:
    """Explicit consent for one endpoint and SHA-256 key, never general trust."""

    endpoint: SshEndpoint
    key_type: str
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, SshEndpoint):
            raise TypeError("endpoint must be an SshEndpoint")
        if self.key_type not in {"ED25519", "ECDSA", "RSA"}:
            raise ValueError("Unsupported approved host-key type")
        if not isinstance(self.fingerprint, str) or not _FINGERPRINT.fullmatch(self.fingerprint):
            raise ValueError("A complete SHA-256 host-key fingerprint is required")

    def matches_question(self, prompt: str) -> bool:
        """Match only a complete local client's unknown-host confirmation question.

        The caller must already have authenticated the IPC peer as the direct
        OpenSSH process. Terminal output is never an input to this decision.
        """
        prefix = "The authenticity of host '"
        if (
            not prompt.startswith(prefix)
            or prompt.rstrip().splitlines()[-1] not in _QUESTION_SUFFIXES
        ):
            return False
        subject, delimiter, remainder = prompt[len(prefix) :].partition("' can't be established.")
        if not delimiter or "\n" in subject or "\r" in subject:
            return False
        # OpenSSH presents '<configured target> (<resolved address>)'. Require
        # the configured endpoint, not merely a matching resolved IP address.
        target, separator, address = subject.partition(" (")
        if not separator or not address.endswith(")"):
            return False
        if (
            target.casefold()
            != known_hosts_target(self.endpoint.host, self.endpoint.port).casefold()
        ):
            return False
        keys = _KEY_LINE.findall(remainder)
        return keys == [(self.key_type, self.fingerprint)]
