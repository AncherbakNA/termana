"""Strict local-only tunnel settings; launch approval is never serialized."""

from typing import Final

from termana.models.ssh_tunnels import MAX_SSH_LOCAL_FORWARDS, SshLocalForward, SshTunnelOptions

SSH_TUNNEL_FIELDS: Final = frozenset({"local_forwards"})
SSH_LOCAL_FORWARD_FIELDS: Final = frozenset({"local_port", "destination_host", "destination_port"})


def ssh_tunnels_to_document(options: SshTunnelOptions) -> dict[str, object]:
    """Encode only validated ordered rules, without listen addresses or consent."""
    if type(options) is not SshTunnelOptions:
        raise ValueError("SSH tunnels must be SshTunnelOptions")
    return {
        "local_forwards": [
            {
                "local_port": forward.local_port,
                "destination_host": forward.destination_host,
                "destination_port": forward.destination_port,
            }
            for forward in options.local_forwards
        ]
    }


def ssh_tunnels_from_document(value: object) -> SshTunnelOptions:
    """Reject unknown fields and excessive collections before constructing rules."""
    if type(value) is not dict or set(value) != SSH_TUNNEL_FIELDS:
        raise ValueError("ssh_tunnels fields do not match the supported schema")
    forwards = value["local_forwards"]
    if type(forwards) is not list:
        raise ValueError("SSH local forwards must be a JSON array")
    if len(forwards) > MAX_SSH_LOCAL_FORWARDS:
        raise ValueError(f"At most {MAX_SSH_LOCAL_FORWARDS} local SSH tunnels are supported")
    rules: list[SshLocalForward] = []
    for forward in forwards:
        if type(forward) is not dict or set(forward) != SSH_LOCAL_FORWARD_FIELDS:
            raise ValueError("SSH local forward fields do not match the supported schema")
        rules.append(SshLocalForward(**forward))
    return SshTunnelOptions(tuple(rules))
