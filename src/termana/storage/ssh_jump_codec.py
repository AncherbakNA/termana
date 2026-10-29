"""Strict complete JSON routing records shared by local and portable storage."""

from typing import Final

from termana.models.ssh_jump import SshJumpMode, SshJumpOptions

SSH_JUMP_FIELDS: Final = frozenset({"mode", "host", "port", "username"})


def ssh_jump_to_document(jump: SshJumpOptions) -> dict[str, str | int | None]:
    """Encode one typed single-hop route, including all nullable fields."""
    if type(jump) is not SshJumpOptions:
        raise ValueError("SSH jump options must be SshJumpOptions")
    return {
        "mode": jump.mode.value,
        "host": jump.host,
        "port": jump.port,
        "username": jump.username,
    }


def ssh_jump_from_document(value: object) -> SshJumpOptions:
    """Reject absent, unknown, or loosely typed fields before model validation."""
    if type(value) is not dict:
        raise ValueError("ssh_jump must be a JSON object")
    if set(value) != SSH_JUMP_FIELDS:
        raise ValueError("ssh_jump fields do not match the supported schema")
    if type(value["mode"]) is not str:
        raise ValueError("SSH jump mode must be a string")
    try:
        mode = SshJumpMode(value["mode"])
    except ValueError as error:
        raise ValueError("Unsupported SSH jump mode") from error
    return SshJumpOptions(
        mode=mode,
        host=value["host"],
        port=value["port"],
        username=value["username"],
    )
