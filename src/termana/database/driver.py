"""DB-API adapters; SQL and wire protocols remain database/driver owned."""

import socket
import sqlite3
import stat
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from termana.database.network import BudgetFile, DatabaseTls, ReplyBudget
from termana.database.results import (
    MAX_QUERY_BYTES,
    MAX_RESULT_COLUMNS,
    DatabaseComponentError,
    collect_result,
)
from termana.sftp.sftp_tree_local import is_link, local_directory

REVIEWED_PG8000_VERSION = "1.31.6"
REVIEWED_PYMYSQL_VERSION = "1.2.3"


class DatabaseDriver:
    def __init__(self, profile, owner):
        self.profile, self.options, self.owner = profile, profile.database_options, owner
        self.connection = None
        self.budget = ReplyBudget()
        self._local_guard = None

    def connect(self, username, password):
        options = self.options
        if options.engine == "sqlite":
            self._connect_sqlite()
            return
        if options.engine == "postgresql":
            import pg8000.dbapi
            if version("pg8000") != REVIEWED_PG8000_VERSION:
                raise DatabaseComponentError
        else:
            import pymysql
            if version("PyMySQL") != REVIEWED_PYMYSQL_VERSION:
                raise DatabaseComponentError
        context = DatabaseTls(self.owner, options.ca_file) if options.tls_mode == "verify-full" else False
        self.owner.check_stopped()
        sock = socket.create_connection((self.profile.host, self.profile.port), options.timeout)
        self.owner.track_socket(sock)
        if options.engine == "postgresql":
            connection = pg8000.dbapi.Connection(
                username, host=self.profile.host, port=self.profile.port,
                database=options.database or None, password=password, sock=sock,
                ssl_context=context, timeout=options.timeout, application_name="Termana",
                startup_params={"statement_timeout": str(options.query_timeout * 1000),
                                "default_transaction_read_only": "on" if options.read_only else "off"},
            )
            self.connection = connection
            connection.password = None
            connection._sock = BudgetFile(connection._sock, self.budget)
        else:
            class OwnedConnection(pymysql.connections.Connection):
                def _create_ssl_ctx(self, _settings):
                    return context
            connection = OwnedConnection(
                host=self.profile.host, port=self.profile.port, user=username,
                password=password.encode("utf-8"), database=options.database,
                charset="utf8mb4", autocommit=False, local_infile=False,
                connect_timeout=options.timeout, read_timeout=options.query_timeout,
                write_timeout=options.timeout, ssl=context, ssl_disabled=context is False,
                defer_connect=True,
            )
            self.connection = connection
            connection.connect(sock=sock)
            connection.password = b""
            connection._rfile = BudgetFile(connection._rfile, self.budget)
            if options.read_only:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
        self.owner.check_stopped()

    def _connect_sqlite(self):
        options = self.options
        if options.sqlite_path is None or not Path(options.sqlite_path).is_absolute():
            raise ValueError("Choose an existing absolute SQLite database path")
        path = Path(options.sqlite_path)
        self._local_guard = local_directory(path.parent)
        directory = self._local_guard.__enter__()
        before = directory.lstat(path.name)
        if before is None or is_link(before) or not stat.S_ISREG(before.st_mode):
            raise ValueError("Choose a regular SQLite database file, not a link")
        mode = "ro" if options.read_only else "rw"
        connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True,
                                     timeout=min(options.timeout, options.query_timeout, 0.2))
        self.connection = connection
        after = directory.lstat(path.name)
        if after is None or is_link(after) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("The selected SQLite file changed while opening")
        connection.enable_load_extension(False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_QUERY_BYTES)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_RESULT_COLUMNS)
        connection.set_progress_handler(lambda: int(self.owner.stopped), 1000)
        connection.execute("PRAGMA trusted_schema=OFF")
        if options.read_only:
            connection.execute("PRAGMA query_only=ON")
        def authorize(action, one, two, _database, _trigger):
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and str(two or one).lower() in ("load_extension", "readfile", "writefile"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)

    @property
    def transaction_open(self):
        if self.connection is None:
            return False
        if self.options.engine == "sqlite":
            return self.connection.in_transaction
        if self.options.engine == "postgresql":
            return self.connection._in_transaction
        return bool(self.connection.server_status & 1)

    def execute(self, query, parameters=()):
        self.budget.reset()
        self.owner.check_stopped()
        if self.options.engine == "postgresql" and not parameters:
            # The reviewed extended-query API accepts exactly one statement,
            # preserves literal % characters and does not concatenate result sets.
            if not self.connection._in_transaction and not self.connection.autocommit:
                self.connection.execute_unnamed("BEGIN")
            context = self.connection.execute_unnamed(query)
            rows = iter(context.rows or ())
            cursor = SimpleNamespace(
                description=None if context.columns is None else [(c["name"],) for c in context.columns],
                rowcount=context.row_count, fetchone=lambda: next(rows, None))
            return collect_result(cursor, transaction_open=self.transaction_open)
        cursor = self.connection.cursor()
        try:
            if parameters:
                cursor.execute(query, parameters)
            else:
                cursor.execute(query)
            result = collect_result(cursor)
            return replace(result, transaction_open=self.transaction_open)
        finally:
            cursor.close()

    def tables(self):
        if self.options.engine == "postgresql":
            result = self.execute("SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY table_schema, table_name LIMIT 1001")
        elif self.options.engine == "mysql":
            if self.options.database:
                result = self.execute("SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name LIMIT 1001", (self.options.database,))
            else:
                result = self.execute("SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_schema NOT IN ('information_schema','mysql','performance_schema','sys') ORDER BY table_schema, table_name LIMIT 1001")
        else:
            result = self.execute("SELECT 'main', name, type FROM sqlite_schema WHERE type IN ('table','view') AND substr(name,1,7)<>'sqlite_' ORDER BY name LIMIT 1001")
        return result

    def commit(self):
        self.budget.reset()
        self.connection.commit()
        return self.transaction_open

    def columns(self, schema, name):
        if self.options.engine in ("postgresql", "mysql"):
            return self.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position LIMIT 257", (schema, name))
        return self.execute("SELECT name, type, [notnull] FROM pragma_table_info(?) LIMIT 257", (name,))

    def rollback(self):
        self.budget.reset()
        self.connection.rollback()
        return self.transaction_open

    def close(self):
        try:
            if self.connection is not None:
                # DB-API close rolls back unfinished transactions. No commit is
                # issued automatically on disconnect or a failed query.
                self.connection.close()
        finally:
            self.connection = None
            if self._local_guard is not None:
                self._local_guard.__exit__(None, None, None)
                self._local_guard = None


def table_query(engine, schema, name):
    for value in (schema, name):
        if type(value) is not str or not value or "\0" in value or len(value) > 1024:
            raise ValueError("Unsupported database object name")
    def quote(value):
        token = "`" if engine == "mysql" else '"'
        return token + value.replace(token, token * 2) + token
    return "SELECT * FROM " + quote(schema) + "." + quote(name) + " LIMIT 1000"
