"""Strict database profile projection; local file paths never leave the device."""

from dataclasses import asdict

from termana.models.database_options import DatabaseOptions

LOCAL_FIELDS = frozenset(asdict(DatabaseOptions()))
PORTABLE_FIELDS = LOCAL_FIELDS - {"sqlite_path", "ca_file"}


def database_options_to_document(options, *, portable=False):
    if type(options) is not DatabaseOptions:
        raise ValueError("Expected database options")
    fields = PORTABLE_FIELDS if portable else LOCAL_FIELDS
    return {name: getattr(options, name) for name in sorted(fields)}


def database_options_from_document(document, *, portable=False):
    if type(document) is not dict or set(document) != (PORTABLE_FIELDS if portable else LOCAL_FIELDS):
        raise ValueError("Database option fields do not match the supported format")
    return DatabaseOptions(**document)
