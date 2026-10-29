"""Version-gated adapter: reviewed AsyncSSH SFTP protocol over OpenSSH pipes.

OpenSSH owns SSH/config/keys/crypto. This adapter does not call asyncssh.connect,
parse terminal ls output, or implement SFTP wire packets. Only the small reviewed
SFTP stream integration below uses non-public library contracts.
"""

import asyncio
import inspect
import logging

import asyncssh
from asyncssh.logging import SSHLogger
from asyncssh.sftp import start_sftp_client

from termana.credentials.embedded_ssh_auth import REVIEWED_ASYNCSSH_VERSION


def require_system_sftp_contract():
    expected = (
        "conn",
        "loop",
        "utf8_decode_errors",
        "reader",
        "writer",
        "path_encoding",
        "path_errors",
        "sftp_version",
    )
    if (
        asyncssh.__version__ != REVIEWED_ASYNCSSH_VERSION
        or tuple(inspect.signature(start_sftp_client).parameters) != expected
    ):
        raise RuntimeError("The System SFTP adapter needs a library compatibility review")


class PipeReader:
    def __init__(self, stream):
        self.stream = stream
        self.logger = SSHLogger(logging.getLogger("termana.system-sftp-protocol"))
        # Never expose paths/protocol content through this adapter's logger.
        self.logger.logger.disabled = True

    async def readexactly(self, size):
        return await self.stream.readexactly(size)

    def get_extra_info(self, _name, default=None):
        # SSH negotiation belongs to the external process, not this protocol
        # adapter. Do not invent server/banner/crypto metadata from stderr.
        return default


class PipeWriter:
    def __init__(self, process):
        self.process = process
        self.channel = self

    def is_closing(self):
        return self.process.stdin.is_closing()

    def write(self, data):
        self.process.stdin.write(data)

    def write_eof(self):
        self.process.stdin.close()

    close = write_eof

    async def wait_closed(self):
        await self.process.wait()


class SftpTasks:
    def __init__(self):
        self.tasks = set()

    def create_task(self, coroutine, _logger):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            # Consume protocol task failures without logging remote content.
            task.exception()

    async def close(self):
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def open_pipe_sftp(process, tasks):
    require_system_sftp_contract()
    return await start_sftp_client(
        tasks,
        asyncio.get_running_loop(),
        "strict",
        PipeReader(process.stdout),
        PipeWriter(process),
        "utf-8",
        "strict",
        3,
    )
