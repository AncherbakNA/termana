"""Strict, private, atomic persistence for manually saved commands."""

import json
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from termana.models.command_snippet import (
    MAX_COMMAND_SNIPPETS,
    CommandSnippet,
    validate_command_snippets,
)
from termana.storage.atomic_file import replace_file_atomically
from termana.storage.json_objects import reject_duplicate_json_keys

COMMAND_SNIPPET_SCHEMA_VERSION: Final = 4
MAX_COMMAND_SNIPPET_FILE_BYTES: Final = 2 * 1024 * 1024
DOCUMENT_FIELDS: Final = frozenset({"schema_version", "snippets"})
SNIPPET_FIELDS: Final = frozenset({"id", "name", "command"})
TEMPLATE_SNIPPET_FIELDS: Final = SNIPPET_FIELDS | {"template"}
FAVORITE_SNIPPET_FIELDS: Final = TEMPLATE_SNIPPET_FIELDS | {"favorite"}
FAILED_LOAD_WRITE_DISABLED_REASON: Final = (
    "Saved commands could not be loaded safely and cannot be overwritten"
)
FUTURE_SCHEMA_WRITE_DISABLED_REASON: Final = (
    "Saved commands were created by a newer Termana version and cannot be overwritten"
)


class CommandSnippetStoreError(RuntimeError):
    """Raised without saved command contents or uncontrolled parser context."""


class CommandSnippetStore:
    """Load and atomically save one complete immutable command-library snapshot."""

    def __init__(self, path: Path) -> None:
        """Retain an explicit path without creating or reading any files."""
        if not isinstance(path, Path):
            raise TypeError("Saved command path must be a Path")
        self.path = path
        self._write_disabled_reason: str | None = None

    def load(self) -> tuple[CommandSnippet, ...]:
        """Read without rewriting; failures disable writes until a successful reload."""
        try:
            with self.path.open("rb") as snippet_file:
                raw_document = snippet_file.read(MAX_COMMAND_SNIPPET_FILE_BYTES + 1)
        except FileNotFoundError:
            self._write_disabled_reason = None
            return ()
        except OSError:
            self._disable_writes_after_failed_load()
            raise CommandSnippetStoreError("Could not read saved commands") from None

        if len(raw_document) > MAX_COMMAND_SNIPPET_FILE_BYTES:
            self._disable_writes_after_failed_load()
            raise CommandSnippetStoreError("Saved command file exceeds the supported size limit")
        try:
            document = json.loads(
                raw_document.decode("utf-8"),
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (UnicodeError, ValueError, RecursionError):
            self._disable_writes_after_failed_load()
            raise CommandSnippetStoreError("Could not read saved commands: invalid JSON") from None

        if (
            isinstance(document, dict)
            and type(document.get("schema_version")) is int
            and document["schema_version"] > COMMAND_SNIPPET_SCHEMA_VERSION
        ):
            self._write_disabled_reason = FUTURE_SCHEMA_WRITE_DISABLED_REASON
        try:
            snippets = self._snippets_from_document(document)
        except CommandSnippetStoreError:
            self._disable_writes_after_failed_load()
            raise
        except (TypeError, ValueError, RecursionError):
            self._disable_writes_after_failed_load()
            raise CommandSnippetStoreError(
                "Could not read saved commands: invalid snippet data"
            ) from None
        self._write_disabled_reason = None
        return snippets

    def save(self, snippets: tuple[CommandSnippet, ...]) -> None:
        """Validate before touching disk and replace only a complete private file."""
        if self._write_disabled_reason is not None:
            raise CommandSnippetStoreError(self._write_disabled_reason)
        if type(snippets) is not tuple:
            raise CommandSnippetStoreError("Expected a tuple of CommandSnippet values")
        try:
            validated = validate_command_snippets(snippets)
            schema_version = (
                4
                if any("\n" in item.command for item in validated)
                else 3
                if any(item.favorite for item in validated)
                else 2
                if any(item.template for item in validated)
                else 1
            )
            document = {
                "schema_version": schema_version,
                "snippets": [
                    {
                        "id": str(item.snippet_id),
                        "name": item.name,
                        "command": item.command,
                        **({"template": item.template} if schema_version >= 2 else {}),
                        **({"favorite": item.favorite} if schema_version >= 3 else {}),
                    }
                    for item in validated
                ],
            }
            serialized = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except (UnicodeError, TypeError, ValueError, RecursionError):
            raise CommandSnippetStoreError(
                "Could not write saved commands: invalid snippet data"
            ) from None
        if len(serialized) > MAX_COMMAND_SNIPPET_FILE_BYTES:
            raise CommandSnippetStoreError("Saved command file exceeds the supported size limit")

        try:
            replace_file_atomically(self.path, serialized)
        except OSError:
            raise CommandSnippetStoreError("Could not write saved commands") from None

    def _disable_writes_after_failed_load(self) -> None:
        """Preserve a future-schema explanation across subsequent failed reads."""
        if self._write_disabled_reason is None:
            self._write_disabled_reason = FAILED_LOAD_WRITE_DISABLED_REASON

    @staticmethod
    def _snippets_from_document(document: Any) -> tuple[CommandSnippet, ...]:
        """Reject unknown members, loose types, and noncanonical UUIDs."""
        if not isinstance(document, dict) or set(document) != DOCUMENT_FIELDS:
            raise CommandSnippetStoreError("Saved command document fields do not match its schema")
        schema_version = document["schema_version"]
        if type(schema_version) is not int or schema_version not in (1, 2, 3, 4):
            raise CommandSnippetStoreError("Unsupported saved command schema version")
        values = document["snippets"]
        if not isinstance(values, list) or len(values) > MAX_COMMAND_SNIPPETS:
            raise CommandSnippetStoreError("Saved commands must be a bounded JSON list")
        snippets: list[CommandSnippet] = []
        for value in values:
            fields = (
                SNIPPET_FIELDS,
                TEMPLATE_SNIPPET_FIELDS,
                FAVORITE_SNIPPET_FIELDS,
                FAVORITE_SNIPPET_FIELDS,
            )[schema_version - 1]
            if not isinstance(value, dict) or set(value) != fields:
                raise CommandSnippetStoreError("Saved command fields do not match the schema")
            if (
                schema_version < 4
                and isinstance(value["command"], str)
                and any(char in value["command"] for char in ("\r", "\n"))
            ):
                raise CommandSnippetStoreError("Multiline commands require schema version 4")
            identifier = value["id"]
            if type(identifier) is not str:
                raise CommandSnippetStoreError("Saved command ID must be a canonical UUID string")
            snippet_id = UUID(identifier)
            if str(snippet_id) != identifier:
                raise CommandSnippetStoreError("Saved command ID must be a canonical UUID string")
            snippets.append(
                CommandSnippet(
                    value["name"],
                    value["command"],
                    snippet_id,
                    value.get("template", False),
                    value.get("favorite", False),
                )
            )
        return validate_command_snippets(snippets)
