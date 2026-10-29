"""Reusable SSH identities; password values never belong to this model."""

import unicodedata
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from termana.models.ssh_credential import validate_ssh_username


@dataclass(frozen=True, slots=True)
class SshAccount:
    name: str
    username: str
    account_id: UUID = field(default_factory=uuid4)
    generation: int = 0

    def __post_init__(self):
        if (
            type(self.name) is not str
            or not self.name.strip()
            or len(self.name) > 128
            or any(unicodedata.category(c).startswith("C") for c in self.name)
        ):
            raise ValueError("Account name must contain 1-128 printable characters")
        validate_ssh_username(self.username)
        if not isinstance(self.account_id, UUID):
            raise ValueError("Account ID must be a UUID")
        if type(self.generation) is not int or not 0 <= self.generation <= 2**31 - 1:
            raise ValueError("Invalid account generation")
