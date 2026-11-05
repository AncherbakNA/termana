"""Strict credential-free file-client settings for local and portable catalogs."""

from dataclasses import asdict

from termana.models.file_options import FileOptions

FILE_OPTION_FIELDS = frozenset(asdict(FileOptions()))


def file_options_to_document(options):
    if type(options) is not FileOptions:
        raise ValueError("Expected file-client options")
    return asdict(options)


def file_options_from_document(document):
    if type(document) is not dict or set(document) != FILE_OPTION_FIELDS:
        raise ValueError("File-client option fields do not match the supported format")
    return FileOptions(**document)
