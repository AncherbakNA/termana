"""Lossless UTF-8 text projection for a plain editor, not an executable document."""

import codecs
from dataclasses import dataclass

from termana.models.sftp import MAX_SFTP_EDIT_BYTES


@dataclass(frozen=True, slots=True)
class RemoteTextFormat:
    newline: str = "\n"
    bom: bool = False

    def __post_init__(self):
        if self.newline not in ("\n", "\r\n", "\r") or type(self.bom) is not bool:
            raise ValueError("Unsupported text format")

    def encode(self, text):
        if any(value in text for value in ("\0", "\r", "\u2028", "\u2029")):
            raise ValueError("Unsupported edited text separators")
        data = (codecs.BOM_UTF8 if self.bom else b"") + text.replace("\n", self.newline).encode(
            "utf-8"
        )
        if len(data) > MAX_SFTP_EDIT_BYTES:
            raise ValueError("Edited text exceeds 2 MiB")
        return data


def decode_remote_text(data):
    if type(data) is not bytes or len(data) > MAX_SFTP_EDIT_BYTES or b"\0" in data:
        raise ValueError("Open UTF-8 text up to 2 MiB only")
    bom = data.startswith(codecs.BOM_UTF8)
    text = data[len(codecs.BOM_UTF8) :].decode("utf-8") if bom else data.decode("utf-8")
    if "\u2028" in text or "\u2029" in text:
        raise ValueError("Unsupported paragraph separators")
    without_pairs = text.replace("\r\n", "")
    if "\r\n" in text:
        if "\r" in without_pairs or "\n" in without_pairs:
            raise ValueError("Mixed newline formats are not edited losslessly")
        newline = "\r\n"
    elif "\r" in text:
        if "\n" in text:
            raise ValueError("Mixed newline formats are not edited losslessly")
        newline = "\r"
    else:
        newline = "\n"
    normalized = text.replace(newline, "\n")
    format = RemoteTextFormat(newline, bom)
    if format.encode(normalized) != data:
        raise ValueError("The file cannot be edited without changing its format")
    return normalized, format
