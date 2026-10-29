"""Local, non-network preflight and fixed guidance for the primary SSH engine."""

from dataclasses import replace
from enum import StrEnum
from pathlib import Path

from termana.i18n import tr
from termana.models.connection import ConnectionProfile


class EmbeddedPreparationReason(StrEnum):
    SETTINGS = "settings"
    ROUTE = "route"
    KEY_SOURCE = "key-source"
    IDENTITY_PATH = "identity-path"
    AGENT = "agent"
    TUNNEL_CONSENT = "tunnel-consent"
    OPTIONS = "options"
    USERNAME = "username"
    GATEWAY_USERNAME = "gateway-username"


_GUIDANCE = {
    EmbeddedPreparationReason.SETTINGS: "The embedded SSH launch settings are invalid.",
    EmbeddedPreparationReason.ROUTE: (
        "Embedded SSH supports Direct connection or an explicit jump host. "
        "Inherited SSH config still requires system OpenSSH; choose that engine explicitly. "
        "Do not select Direct if this host relies on SSH config."
    ),
    EmbeddedPreparationReason.GATEWAY_USERNAME: (
        "For embedded jump hosts, set the gateway username in routing settings. "
        "The gateway password has its own terminal prompt or saved-login choice; "
        "destination keys and saved passwords are never reused for the gateway."
    ),
    EmbeddedPreparationReason.KEY_SOURCE: (
        "Key only authentication needs an explicitly selected private key or supported SSH agent."
    ),
    EmbeddedPreparationReason.IDENTITY_PATH: (
        "The selected private key path must be absolute on this computer."
    ),
    EmbeddedPreparationReason.AGENT: (
        "The explicit SSH agent is incompatible with this platform or identity policy. "
        "Embedded SSH supports absolute Unix agent sockets, not Windows agents, "
        "Password only, disabled agents or IdentitiesOnly. Use system OpenSSH explicitly "
        "if you need that agent policy."
    ),
    EmbeddedPreparationReason.TUNNEL_CONSENT: "SSH tunnels need confirmation before connecting.",
    EmbeddedPreparationReason.OPTIONS: (
        "The embedded SSH library rejected the selected connection options. "
        "Check the session's cipher and connection settings."
    ),
    EmbeddedPreparationReason.USERNAME: (
        "The embedded SSH library would change this username during normalization. "
        "Termana refused to authenticate as a different account. Use system OpenSSH explicitly."
    ),
}
_NO_FALLBACK = " No system fallback was made."


def embedded_preparation_guidance(error: Exception) -> str:
    """Never expose raw exceptions, filesystem paths or library/remote strings."""
    if isinstance(error, ImportError):
        message = (
            "The required embedded SSH dependency could not be loaded. "
            "Repair the normal Termana installation in the Python environment used to launch it, "
            "or choose system OpenSSH explicitly."
        )
    else:
        reason = getattr(error, "preparation_reason", None)
        message = _GUIDANCE[
            reason
            if isinstance(reason, EmbeddedPreparationReason)
            else EmbeddedPreparationReason.SETTINGS
        ]
        # Keep this lazy so explicit system compatibility can start even when
        # the installation's primary-engine dependency needs repair.
        from termana.credentials.embedded_ssh_auth import EmbeddedAuthContractError

        if isinstance(error, EmbeddedAuthContractError):
            message = (
                "The embedded SSH authentication integration needs a compatibility review "
                "for the installed library. Use the reviewed Termana installation or "
                "choose system OpenSSH explicitly."
            )
    return tr(message) + tr(_NO_FALLBACK)


def embedded_preflight_guidance(
    profile: ConnectionProfile, known_hosts: Path, agent_path: Path | None
) -> str | None:
    """Probe settings without credentials, sockets, config/key reads or mutations.

    A placeholder validates user-independent settings before a login is chosen.
    It is never passed to a backend. Actual tunnel consent is still required by
    the launch flow; this settings-only probe cannot grant it.
    """
    try:
        from termana.credentials.embedded_ssh_auth import require_embedded_auth_contract
        from termana.terminal.embedded_ssh import EmbeddedSshPlan

        require_embedded_auth_contract()
        probe_profile = (
            profile if profile.username is not None else replace(profile, username="preflight")
        )
        EmbeddedSshPlan(
            probe_profile, known_hosts, tunnels_confirmed=True, agent_path=agent_path
        ).connection_options()
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
        return embedded_preparation_guidance(error)
    return None
