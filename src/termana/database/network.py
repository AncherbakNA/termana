"""Owned transport sockets and byte budgets around reviewed database drivers."""

import select
import ssl
import time

import truststore

from termana.database.results import MAX_WIRE_BYTES, ResultLimitError


class ReplyBudget:
    def __init__(self):
        self.remaining = MAX_WIRE_BYTES

    def reset(self):
        self.remaining = MAX_WIRE_BYTES


class BudgetFile:
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, count):
        if count < 0 or count > self.budget.remaining:
            raise ResultLimitError("Database reply exceeds 16 MiB; use a smaller query")
        value = self.stream.read(count)
        self.budget.remaining -= len(value)
        return value


class DatabaseTls:
    def __init__(self, owner, ca_file):
        self.owner = owner
        self.context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if ca_file is not None:
            self.context.load_verify_locations(cafile=ca_file)

    def __getattr__(self, name):
        return getattr(self.context, name)

    def wrap_socket(self, sock, **options):
        wrapped = self.context.wrap_socket(sock, do_handshake_on_connect=False, **options)
        self.owner.track_socket(wrapped)
        timeout = wrapped.gettimeout()
        deadline = time.monotonic() + (timeout or 15)
        wrapped.setblocking(False)
        try:
            while True:
                self.owner.check_stopped()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                try:
                    wrapped.do_handshake()
                    break
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError) as error:
                    reading = isinstance(error, ssl.SSLWantReadError)
                    select.select([wrapped] if reading else [], [] if reading else [wrapped], [],
                                  min(0.1, max(0, deadline - time.monotonic())))
            wrapped.settimeout(timeout)
            return wrapped
        except BaseException:
            wrapped.close()
            raise
