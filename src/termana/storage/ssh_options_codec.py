"""Shared strict JSON shape for portable SSH connection options."""

from typing import Final

from termana.models.ssh_options import SshConnectionOptions

SSH_OPTIONS_FIELDS: Final = frozenset(
    {"connect_timeout", "server_alive_interval", "server_alive_count_max"}
)


def ssh_options_to_document(options: SshConnectionOptions) -> dict[str, int | None]:
    """Encode all three fields, including null defaults, without identity data."""
    if type(options) is not SshConnectionOptions:
        raise ValueError("SSH options must be SshConnectionOptions")
    return {
        "connect_timeout": options.connect_timeout,
        "server_alive_interval": options.server_alive_interval,
        "server_alive_count_max": options.server_alive_count_max,
    }


def ssh_options_from_document(value: object) -> SshConnectionOptions:
    """Require one complete JSON object and let the model validate exact integers."""
    if type(value) is not dict:
        raise ValueError("ssh_options must be a JSON object")
    if set(value) != SSH_OPTIONS_FIELDS:
        raise ValueError("ssh_options fields do not match the supported schema")
    return SshConnectionOptions(
        connect_timeout=value["connect_timeout"],
        server_alive_interval=value["server_alive_interval"],
        server_alive_count_max=value["server_alive_count_max"],
    )
