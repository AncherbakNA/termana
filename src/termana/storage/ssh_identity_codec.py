"""Strict local JSON shape for SSH identity references, never key material."""

from typing import Final

from termana.models.ssh_identity import SshAuthenticationMode, SshIdentityOptions

SSH_IDENTITY_FIELDS: Final = frozenset({"identity_file", "identities_only", "agent_disabled"})


def ssh_identity_to_document(identity: SshIdentityOptions) -> dict[str, str | bool | None]:
    """Encode every local-only field, including null and false defaults."""
    if type(identity) is not SshIdentityOptions:
        raise ValueError("SSH identity must be SshIdentityOptions")
    document = {
        "identity_file": identity.identity_file,
        "identities_only": identity.identities_only,
        "agent_disabled": identity.agent_disabled,
    }
    if identity.authentication is not SshAuthenticationMode.CONFIG:
        document["authentication"] = identity.authentication.value
    return document


def ssh_identity_from_document(value: object) -> SshIdentityOptions:
    """Require the complete local object before applying pure model validation."""
    if type(value) is not dict:
        raise ValueError("ssh_identity must be a JSON object")
    if set(value) not in (SSH_IDENTITY_FIELDS, SSH_IDENTITY_FIELDS | {"authentication"}):
        raise ValueError("ssh_identity fields do not match the supported schema")
    mode = value.get("authentication", "config")
    if type(mode) is not str:
        raise ValueError("ssh_identity authentication must be text")
    try:
        authentication = SshAuthenticationMode(mode)
    except ValueError:
        raise ValueError("ssh_identity authentication is unsupported") from None
    return SshIdentityOptions(
        identity_file=value["identity_file"],
        identities_only=value["identities_only"],
        agent_disabled=value["agent_disabled"],
        authentication=authentication,
    )
