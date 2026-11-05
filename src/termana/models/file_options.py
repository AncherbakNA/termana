"""Public file-client preferences; credentials and trust have no representation."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileOptions:
    ftp_security: str = "plain"
    ftp_encoding: str = "utf-8"
    timeout: int = 15
    tftp_block_size: int = 512
    tftp_retries: int = 3

    def __post_init__(self):
        if type(self.ftp_security) is not str or self.ftp_security not in ("plain", "explicit-tls"):
            raise ValueError("Choose FTP or explicit FTP over TLS")
        if type(self.ftp_encoding) is not str or self.ftp_encoding not in ("utf-8", "latin-1", "cp1251"):
            raise ValueError("Unsupported FTP filename encoding")
        for name, minimum, maximum in (
            ("timeout", 1, 600), ("tftp_block_size", 8, 65464), ("tftp_retries", 0, 10),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Invalid file-client {name}")

    @property
    def is_default(self):
        return self == FileOptions()


DEFAULT_FILE_OPTIONS = FileOptions()
