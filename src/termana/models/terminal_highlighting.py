"""Bounded, literal-only display highlighting; never rewrites terminal output."""

import ipaddress
import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class HighlightRule:
    name: str
    words: tuple[str, ...]
    color: str
    enabled: bool = True
    addresses: bool = False

    def __post_init__(self):
        if type(self.name) is not str or not self.name.strip() or len(self.name) > 48:
            raise ValueError("Rule name must contain 1-48 characters")
        if any(ord(char) < 32 for char in self.name):
            raise ValueError("Rule name contains control characters")
        if type(self.words) is not tuple or len(self.words) > 64:
            raise ValueError("A rule supports up to 64 words")
        for word in self.words:
            if type(word) is not str or not re.fullmatch(r"[\w.-]{1,48}", word):
                raise ValueError(
                    "Use words containing letters, digits, dots, hyphens or underscores"
                )
        if type(self.color) is not str or not re.fullmatch(r"#[0-9a-fA-F]{6}", self.color):
            raise ValueError("Color must be #RRGGBB")
        if type(self.enabled) is not bool or type(self.addresses) is not bool:
            raise TypeError("Rule switches must be booleans")


DEFAULT_HIGHLIGHT_RULES = (
    HighlightRule(
        "Errors",
        (
            "error",
            "errors",
            "failed",
            "failure",
            "fatal",
            "panic",
            "critical",
            "denied",
            "refused",
            "unreachable",
            "exception",
        ),
        "#ef5350",
    ),
    HighlightRule(
        "Warnings",
        ("warn", "warning", "warnings", "deprecated", "timeout", "retry", "unstable"),
        "#e69522",
    ),
    HighlightRule(
        "Success",
        ("success", "successful", "succeeded", "passed", "ok", "healthy", "connected", "enabled"),
        "#37a66b",
    ),
    HighlightRule("Information", ("info", "notice", "debug", "trace"), "#399cce"),
    HighlightRule("IP addresses", (), "#ae75da", addresses=True),
)


@dataclass(frozen=True, slots=True)
class TerminalHighlighting:
    enabled: bool = True
    rules: tuple[HighlightRule, ...] = DEFAULT_HIGHLIGHT_RULES

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise TypeError("Highlighting switch must be a bool")
        if type(self.rules) is not tuple or len(self.rules) > 16:
            raise ValueError("Highlighting supports up to 16 rules")
        if any(type(rule) is not HighlightRule for rule in self.rules):
            raise TypeError("Expected HighlightRule values")

    def to_data(self):
        return {
            "enabled": self.enabled,
            "rules": [
                {
                    "name": rule.name,
                    "words": list(rule.words),
                    "color": rule.color,
                    "enabled": rule.enabled,
                    "addresses": rule.addresses,
                }
                for rule in self.rules
            ],
        }

    @classmethod
    def from_data(cls, data):
        if type(data) is not dict or set(data) != {"enabled", "rules"}:
            raise ValueError("Invalid highlighting settings")
        if type(data["rules"]) is not list or len(data["rules"]) > 16:
            raise ValueError("Invalid highlighting rules")
        rules = []
        for rule in data["rules"]:
            if type(rule) is not dict or set(rule) != {
                "name",
                "words",
                "color",
                "enabled",
                "addresses",
            }:
                raise ValueError("Invalid highlighting rule fields")
            if type(rule["words"]) is not list:
                raise ValueError("Expected a list of words")
            rules.append(HighlightRule(**{**rule, "words": tuple(rule["words"])}))
        return cls(data["enabled"], tuple(rules))


class LineHighlighter:
    """Per-view bounded cache and compiled literal matchers, not user regexes."""

    def __init__(self, settings=None):
        settings = TerminalHighlighting() if settings is None else settings
        self.settings = settings
        self.patterns = tuple(
            (
                rule,
                re.compile(
                    r"(?<!\w)(?:" + "|".join(re.escape(word) for word in rule.words) + r")(?!\w)",
                    re.IGNORECASE,
                ),
            )
            for rule in settings.rules
            if rule.enabled and rule.words and not rule.addresses
        )
        self.address_rules = tuple(
            rule for rule in settings.rules if rule.enabled and rule.addresses
        )
        self._cached_ranges = lru_cache(maxsize=128)(self._ranges)

    def ranges(self, text):
        return self._cached_ranges(text[:16384])

    def _ranges(self, text):
        if not self.settings.enabled:
            return ()
        # Terminal geometry bounds text already; also cap standalone callers.
        result = []
        for rule, pattern in self.patterns:
            result.extend(
                (match.start(), match.end(), rule.color) for match in pattern.finditer(text)
            )
        for rule in self.address_rules:
            for match in re.finditer(
                r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\w|\.\d)|(?<![\w.:])(?=[0-9a-fA-F:.]{0,44}:)[0-9a-fA-F:.]{2,45}(?![\w.:])",
                text,
            ):
                candidate = match.group().rstrip(".")
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                result.append((match.start(), match.start() + len(candidate), rule.color))
        return tuple(result)
