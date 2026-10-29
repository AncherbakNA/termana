"""Application-owned process state, never evidence of SSH authentication."""

from dataclasses import dataclass

from termana.models.open_terminal import OpenTerminalState


@dataclass(frozen=True, slots=True)
class SshSessionStatus:
    state: OpenTerminalState
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not OpenTerminalState:
            raise TypeError("SSH process state must be OpenTerminalState")
        if self.state is OpenTerminalState.ENDED:
            if type(self.exit_code) is not int or not -(2**31) <= self.exit_code <= 2**32 - 1:
                raise ValueError("Ended SSH process requires an integer exit code")
        elif self.exit_code is not None:
            raise ValueError("A process which has not ended cannot have an exit code")
