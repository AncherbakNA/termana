"""Public database preferences and explicit local file references; no secrets."""

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class DatabaseOptions:
    engine: str = "postgresql"
    database: str = ""
    tls_mode: str = "verify-full"
    timeout: int = 15
    query_timeout: int = 30
    read_only: bool = True
    sqlite_path: str | None = None
    ca_file: str | None = None

    def __post_init__(self):
        if type(self.engine) is not str or self.engine not in ("postgresql", "mysql", "sqlite"):
            raise ValueError("Choose PostgreSQL, MySQL/MariaDB or SQLite")
        if (type(self.database) is not str or len(self.database.encode("utf-8")) > 512
                or any(ord(c) < 32 or ord(c) == 127 for c in self.database)):
            raise ValueError("Database name must be plain text up to 512 bytes")
        if type(self.tls_mode) is not str or self.tls_mode not in ("verify-full", "disabled"):
            raise ValueError("Choose verified TLS or an explicitly unencrypted connection")
        if type(self.read_only) is not bool:
            raise ValueError("Database read-only preference must be boolean")
        for value in (self.timeout, self.query_timeout):
            if type(value) is not int or not 1 <= value <= 600:
                raise ValueError("Database timeouts must be between 1 and 600 seconds")
        for value in (self.sqlite_path, self.ca_file):
            if value is not None and (type(value) is not str or not value
                    or len(value.encode("utf-8")) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError("Database local paths must be plain text up to 4096 bytes")
        if self.engine != "sqlite" and self.sqlite_path is not None:
            raise ValueError("Only SQLite can select a database file")
        if self.engine == "sqlite" and (self.database or self.ca_file is not None
                                      or self.tls_mode != "verify-full"):
            raise ValueError("SQLite does not select a network database or TLS settings")

    @property
    def is_default(self):
        return self == DatabaseOptions()

    def portable(self):
        return replace(self, sqlite_path=None, ca_file=None)


DEFAULT_DATABASE_OPTIONS = DatabaseOptions()
