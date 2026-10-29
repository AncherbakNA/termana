"""Validated, explicitly saved terminal commands with bounded line breaks."""

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

MAX_COMMAND_SNIPPETS: Final = 200
MAX_SNIPPET_NAME_CHARACTERS: Final = 80
MAX_SNIPPET_COMMAND_CHARACTERS: Final = 2_048


def _has_unsafe_characters(value: str) -> bool:
    """Reject controls, format characters, and every Unicode line separator."""
    return any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _validated_snippet_name(name: str) -> str:
    """Return a trimmed NFC display label without concealing unsafe input."""
    if type(name) is not str:
        raise TypeError("Saved command name must be a string")
    if _has_unsafe_characters(name):
        raise ValueError("Saved command name contains unsafe control or formatting characters")
    normalized = unicodedata.normalize("NFC", name.strip())
    if not normalized:
        raise ValueError("Saved command name cannot be empty")
    if len(normalized) > MAX_SNIPPET_NAME_CHARACTERS:
        raise ValueError(
            f"Saved command name cannot exceed {MAX_SNIPPET_NAME_CHARACTERS} characters"
        )
    return normalized


def validate_snippet_command(command: str) -> None:
    """Validate one command without stripping, normalizing, or otherwise editing it."""
    if type(command) is not str:
        raise TypeError("Saved command must be a string")
    if not command or command.isspace():
        raise ValueError("Saved command cannot be empty or only whitespace")
    if len(command) > MAX_SNIPPET_COMMAND_CHARACTERS:
        raise ValueError(f"Saved command cannot exceed {MAX_SNIPPET_COMMAND_CHARACTERS} characters")
    normalized = command.replace("\r\n", "\n").replace("\r", "\n")
    if _has_unsafe_characters(normalized.replace("\n", "")):
        raise ValueError(
            "Saved command cannot contain control or formatting characters other than line breaks"
        )


@dataclass(frozen=True, slots=True)
class CommandSnippet:
    """One immutable manual command, omitted from diagnostic representations."""

    name: str
    command: str = field(repr=False)
    snippet_id: UUID = field(default_factory=uuid4)
    template: bool = False
    favorite: bool = False

    def __post_init__(self) -> None:
        """Normalize only the display label and validate the literal command."""
        object.__setattr__(self, "name", _validated_snippet_name(self.name))
        validate_snippet_command(self.command)
        object.__setattr__(
            self, "command", self.command.replace("\r\n", "\n").replace("\r", "\n")
        )
        if type(self.snippet_id) is not UUID:
            raise TypeError("Saved command ID must be a UUID")
        if type(self.template) is not bool:
            raise TypeError("Template mode must be a boolean")
        if type(self.favorite) is not bool:
            raise TypeError("Favorite mark must be a boolean")
        if self.template:
            from termana.models.command_template import command_parameters

            command_parameters(self.command)


def validate_command_snippets(snippets: Iterable[CommandSnippet]) -> tuple[CommandSnippet, ...]:
    """Return a bounded snapshot with unique IDs and normalized casefolded names."""
    result: list[CommandSnippet] = []
    identifiers: set[UUID] = set()
    names: set[str] = set()
    for snippet in snippets:
        if len(result) >= MAX_COMMAND_SNIPPETS:
            raise ValueError(f"Cannot save more than {MAX_COMMAND_SNIPPETS} commands")
        if type(snippet) is not CommandSnippet:
            raise TypeError("Saved command collection must contain only CommandSnippet values")
        if _validated_snippet_name(snippet.name) != snippet.name:
            raise ValueError("Saved command name must be trimmed and NFC-normalized")
        validate_snippet_command(snippet.command)
        if "\r" in snippet.command:
            raise ValueError("Saved commands must use normalized line endings")
        if type(snippet.snippet_id) is not UUID:
            raise TypeError("Saved command ID must be a UUID")
        if type(snippet.template) is not bool:
            raise TypeError("Template mode must be a boolean")
        if type(snippet.favorite) is not bool:
            raise TypeError("Favorite mark must be a boolean")
        if snippet.template:
            from termana.models.command_template import command_parameters

            command_parameters(snippet.command)
        if snippet.snippet_id in identifiers:
            raise ValueError("Saved command IDs must be unique")
        name_key = unicodedata.normalize("NFC", snippet.name.casefold())
        if name_key in names:
            raise ValueError("Saved command names must be unique, ignoring case")
        identifiers.add(snippet.snippet_id)
        names.add(name_key)
        result.append(snippet)
    return tuple(result)


def duplicate_command_snippet(
    source: CommandSnippet, snippets: Iterable[CommandSnippet]
) -> CommandSnippet:
    """Create an unsaved, independent draft with a bounded collision-free name."""
    snapshot = validate_command_snippets(snippets)
    if type(source) is not CommandSnippet or source not in snapshot:
        raise ValueError("Choose an existing saved command to duplicate")
    if len(snapshot) >= MAX_COMMAND_SNIPPETS:
        raise ValueError(f"Cannot save more than {MAX_COMMAND_SNIPPETS} commands")
    reserved = {unicodedata.normalize("NFC", item.name.casefold()) for item in snapshot}
    for number in range(1, len(snapshot) + 2):
        suffix = " (copy)" if number == 1 else f" (copy {number})"
        name = source.name[: MAX_SNIPPET_NAME_CHARACTERS - len(suffix)].rstrip() + suffix
        if unicodedata.normalize("NFC", name.casefold()) not in reserved:
            return CommandSnippet(name=name, command=source.command, template=source.template)
    raise ValueError("Could not choose a unique saved command name")
