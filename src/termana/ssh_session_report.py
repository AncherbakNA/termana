"""Pure bounded SSH report export; identifiers are always hidden by construction."""

from typing import Final

from termana.models.connection import ConnectionProfile
from termana.models.ssh_negotiation import SshNegotiatedFacts
from termana.models.ssh_session_status import SshSessionStatus
from termana.ssh_session_details import ssh_session_details_text

MAX_SSH_SESSION_REPORT_BYTES: Final = 64 * 1024
_HEADER: Final = "TERMANA SSH SESSION REPORT · REDACTED\n"


def ssh_session_report_bytes(
    profile: ConnectionProfile,
    *,
    status: SshSessionStatus | None = None,
    negotiation: SshNegotiatedFacts | None = None,
) -> bytes:
    """Encode allowlisted immutable fields, never displayed text, files or output.

    There is deliberately no sensitive/unredacted mode. This is a bounded report,
    not a recording, current transport-health probe or anonymity guarantee.
    """
    text = ssh_session_details_text(profile, redacted=True, status=status, negotiation=negotiation)
    if "\0" in text:
        raise ValueError("SSH report could not be encoded safely")
    try:
        data = (_HEADER + text + "\n").encode("utf-8")
    except UnicodeError:
        raise ValueError("SSH report could not be encoded safely") from None
    if len(data) > MAX_SSH_SESSION_REPORT_BYTES:
        raise ValueError("SSH report exceeds its export size limit")
    return data
