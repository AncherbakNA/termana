"""Bounded, opt-in literal parameters; never an expression or shell interpreter."""

import re
import unicodedata
from collections.abc import Mapping
from typing import Final

MAX_TEMPLATE_PARAMETERS: Final = 12
MAX_PARAMETER_CHARACTERS: Final = 512
MAX_PARAMETER_OCCURRENCES: Final = 64
_PARAMETER = re.compile(r"(?<!\{)\{\{([A-Za-z_][A-Za-z0-9_]{0,31})\}\}(?!\})")


def command_parameters(command: str) -> tuple[str, ...]:
    """Return unique ASCII names in display order, rejecting ambiguous braces."""
    from termana.models.command_snippet import validate_snippet_command

    validate_snippet_command(command)
    matches = tuple(_PARAMETER.finditer(command))
    remainder = _PARAMETER.sub("", command)
    if not matches or "{{" in remainder or "}}" in remainder:
        raise ValueError("Use {{name}} parameters with ASCII letters, digits and underscores.")
    names = tuple(dict.fromkeys(match[1] for match in matches))
    if len(names) > MAX_TEMPLATE_PARAMETERS or len(matches) > MAX_PARAMETER_OCCURRENCES:
        raise ValueError("This command has too many parameters or repeated placeholders.")
    return names


def render_command_template(command: str, values: Mapping[str, str]) -> str:
    """Substitute exact values once, with no recursion, quoting, expansion or evaluation."""
    from termana.models.command_snippet import validate_snippet_command

    names = command_parameters(command)
    if not isinstance(values, Mapping) or set(values) != set(names):
        raise ValueError("Enter exactly the parameters requested by this command.")
    for value in values.values():
        if type(value) is not str or len(value) > MAX_PARAMETER_CHARACTERS:
            raise ValueError(f"Each parameter is limited to {MAX_PARAMETER_CHARACTERS} characters.")
        if any(
            unicodedata.category(char).startswith("C") or unicodedata.category(char) in {"Zl", "Zp"}
            for char in value
        ):
            raise ValueError(
                "Parameters must be one line without control or formatting characters."
            )
    rendered = _PARAMETER.sub(lambda match: values[match[1]], command)
    validate_snippet_command(rendered)
    return rendered
