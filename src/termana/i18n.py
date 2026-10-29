"""Explicit process UI language; never translate terminal output or user data."""

from contextlib import contextmanager

from termana.models.settings import InterfaceLanguage

LANGUAGE_LABELS = {
    InterfaceLanguage.ENGLISH: "English",
}
_language = InterfaceLanguage.ENGLISH


def current_language() -> InterfaceLanguage:
    """Return the active launch language, not the next-launch preference."""
    return _language


def tr(source: str, *, context: str | None = None, **values: object) -> str:
    """Translate only an explicit source template; inserted data stays literal."""
    key = f"{context}:{source}" if context is not None else source
    if _language is InterfaceLanguage.RUSSIAN:
        # Dormant catalog support is retained for future translation work;
        # normal English-only startup neither loads nor installs it.
        from termana.translations.ru import RUSSIAN

        selected = RUSSIAN.get(key, source)
    else:
        selected = source
    return selected.format_map(values) if values else selected


def validation_text(message: str) -> str:
    """Translate only registered local validation diagnostics, not arbitrary errors/data."""
    return tr(message, context="validation")


def count_noun(one: str, many: str, count: int) -> str:
    """Select a UI count noun; never use this for command or identifier data."""
    if _language is InterfaceLanguage.ENGLISH:
        return one if count == 1 else many
    remainder = abs(count) % 100
    form = (
        "one"
        if remainder % 10 == 1 and remainder != 11
        else "few"
        if 2 <= remainder % 10 <= 4 and not 12 <= remainder <= 14
        else "many"
    )
    return tr(one if form == "one" else many, context=f"count-{form}")


@contextmanager
def application_language(application, language: InterfaceLanguage):
    """Activate for one GUI lifetime; changing Preferences never mutates live owners."""
    global _language
    if type(language) is not InterfaceLanguage:
        raise TypeError("language must be an InterfaceLanguage")
    from PySide6.QtCore import QLibraryInfo, QTranslator

    previous = _language
    translator = None
    _language = language
    try:
        if language is InterfaceLanguage.RUSSIAN:
            translator = QTranslator(application)
            directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
            if translator.load("qtbase_ru", directory):
                application.installTranslator(translator)
        yield
    finally:
        if translator is not None:
            application.removeTranslator(translator)
        _language = previous
