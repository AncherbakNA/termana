"""Pure display and search labels for supported Telnet text encodings."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from termana.models.connection import TelnetEncoding


@dataclass(frozen=True, slots=True)
class _TelnetEncodingPresentation:
    """Keep one compact sidebar label aligned with its user-visible aliases."""

    label: str
    dialog_label: str


_TELNET_ENCODING_PRESENTATIONS: Final[Mapping[TelnetEncoding, _TelnetEncodingPresentation]] = (
    MappingProxyType(
        {
            TelnetEncoding.UTF8: _TelnetEncodingPresentation(
                "UTF-8",
                "UTF-8 (recommended)",
            ),
            TelnetEncoding.WINDOWS_1251: _TelnetEncodingPresentation(
                "CP1251",
                "Windows-1251 (Cyrillic)",
            ),
            TelnetEncoding.DOS_866: _TelnetEncodingPresentation(
                "CP866",
                "DOS CP866 (Cyrillic)",
            ),
            TelnetEncoding.KOI8_R: _TelnetEncodingPresentation(
                "KOI8-R",
                "KOI8-R (Cyrillic)",
            ),
            TelnetEncoding.LATIN_1: _TelnetEncodingPresentation(
                "ISO-8859-1",
                "ISO-8859-1 (Western European)",
            ),
        }
    )
)


def telnet_encoding_label(encoding: TelnetEncoding) -> str:
    """Return the compact encoding label shown for one saved Telnet profile."""
    return _telnet_encoding_presentation(encoding).label


def telnet_encoding_dialog_label(encoding: TelnetEncoding) -> str:
    """Return the complete label used by the saved-connection editor."""
    return _telnet_encoding_presentation(encoding).dialog_label


def telnet_encoding_search_aliases(encoding: TelnetEncoding) -> tuple[str, ...]:
    """Return persistence and visible names accepted by saved-session search."""
    presentation = _telnet_encoding_presentation(encoding)
    return tuple(
        dict.fromkeys(
            (
                encoding.value,
                presentation.label,
                presentation.dialog_label,
            )
        )
    )


def _telnet_encoding_presentation(
    encoding: TelnetEncoding,
) -> _TelnetEncodingPresentation:
    """Resolve only a typed, allowlisted encoding to presentation metadata."""
    if not isinstance(encoding, TelnetEncoding):
        raise TypeError("encoding must be a TelnetEncoding")
    return _TELNET_ENCODING_PRESENTATIONS[encoding]
