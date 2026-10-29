"""Explicit protocol choice and ordered SSH2 overrides, never config fragments."""

import re
from dataclasses import dataclass

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class SshAlgorithms:
    kex: tuple[str, ...] = ()
    host_keys: tuple[str, ...] = ()
    protocol: str = "auto"

    def __post_init__(self) -> None:
        for names in (self.kex, self.host_keys):
            if (
                type(names) is not tuple
                or len(names) > 32
                or any(
                    type(n) is not str or _NAME.fullmatch(n) is None or n.lower() == "none"
                    for n in names
                )
                or len(set(names)) != len(names)
            ):
                raise ValueError(
                    "Use up to 32 unique SSH algorithm names, without config operators"
                )
        if type(self.protocol) is not str or self.protocol not in ("auto", "2", "1"):
            raise ValueError("Choose Automatic (SSH2), SSH2 or explicit legacy SSH1")

    @property
    def is_default(self) -> bool:
        return self == SshAlgorithms()


def algorithms_to_document(value: SshAlgorithms) -> dict:
    return dict(kex=list(value.kex), host_keys=list(value.host_keys), protocol=value.protocol)


def algorithms_from_document(value: object, *, allow_ssh1: bool = True) -> SshAlgorithms:
    if (
        type(value) is not dict
        or set(value) != {"kex", "host_keys", "protocol"}
        or type(value["kex"]) is not list
        or type(value["host_keys"]) is not list
    ):
        raise ValueError("Invalid SSH algorithm settings")
    result = SshAlgorithms(tuple(value["kex"]), tuple(value["host_keys"]), value["protocol"])
    if result.protocol == "1" and not allow_ssh1:
        raise ValueError("SSH1 requires the new explicit-protocol schema")
    return result


DEFAULT_SSH_ALGORITHMS = SshAlgorithms()
