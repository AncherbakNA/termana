"""An explicit independent OpenSSH file connection; no terminal/cipher fallback."""

import asyncio
import hmac
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from termana.helper_runtime import helper_command
from termana.models.connection import ConnectionProfile
from termana.terminal.command import ShellCommand
from termana.terminal.ssh_command import build_ssh_command, resolve_system_ssh_program
from termana.terminal.system_sftp_askpass import CAPABILITY_ENV
from termana.terminal.system_sftp_stream import (
    SftpTasks,
    open_pipe_sftp,
    require_system_sftp_contract,
)

SYSTEM_SFTP_LOGIN_SECONDS = 120
SYSTEM_SFTP_CLOSE_SECONDS = 3
MAX_SYSTEM_SFTP_RESPONSES = 8


def system_sftp_command(profile, program, *, known_hosts=None, system_config=None):
    if type(profile) is not ConnectionProfile or not os.path.isabs(program):
        raise ValueError("System SFTP requires a validated SSH profile and system executable")
    command = build_ssh_command(
        host=profile.host,
        port=profile.port,
        username=profile.username,
        program=program,
        ssh_cipher=profile.ssh_cipher,
        ssh_algorithms=profile.ssh_algorithms,
        ssh_options=profile.ssh_options,
        ssh_identity=profile.ssh_identity,
        ssh_jump=profile.ssh_jump,
        known_hosts=known_hosts,
        system_config=system_config,
    )
    boundary = command.arguments.index("--")
    return (
        program,
        "-T",
        "-s",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "UpdateHostKeys=no",
        # Inbox Windows OpenSSH predates these config keywords. Its private
        # kill-on-close Job already retires any child when the owner exits.
        *(("-o", "ForkAfterAuthentication=no", "-o", "StdinNull=no") if os.name != "nt" else ()),
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPersist=no",
        "-S",
        "none",
        "-o",
        "NumberOfPasswordPrompts=3",
        "-o",
        "LogLevel=ERROR",
        *command.arguments[:boundary],
        "--",
        *command.arguments[boundary + 1 :],
        "sftp",
    )


class SystemSftpConnection:
    def __init__(self, profile, program, response, *, known_hosts=None, system_config=None):
        require_system_sftp_contract()
        if resolve_system_ssh_program() != program:
            raise ValueError("System SFTP can only run the trusted OS OpenSSH executable")
        self.argv = system_sftp_command(profile, program, known_hosts=known_hosts, system_config=system_config)
        self.cwd = None if system_config is None else str(system_config.parent)
        self.response = response
        self.process = None
        self.server = None
        self.directory = None
        self.tasks = SftpTasks()
        self.client = None
        self.stderr_task = None
        self.responses = 0
        self.clients = set()
        self.token = secrets.token_hex(32)
        self.authenticated = False
        self._closed = False

    async def _respond(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            if self._closed or self.authenticated or len(self.clients) > 1:
                return
            async with asyncio.timeout(SYSTEM_SFTP_LOGIN_SECONDS):
                request = json.loads(await asyncio.wait_for(reader.readline(), 3))
                if type(request) is not dict:
                    return
                token = request.get("token")
                if type(token) is not str or not hmac.compare_digest(token, self.token):
                    return
                prompt = request.get("prompt")
                if type(prompt) is not str or len(prompt.encode("utf-8")) > 4096:
                    return
                self.responses += 1
                if self.responses > MAX_SYSTEM_SFTP_RESPONSES:
                    self._terminate()
                    return
                answer = await self.response(prompt)
                if answer is None:
                    self._terminate()
                    return
                if (
                    type(answer) is not str
                    or any(c in answer for c in "\r\n\0")
                    or len(answer.encode("utf-8")) > 1023
                ):
                    self._terminate()
                    return
                writer.write(json.dumps({"answer": answer}).encode("utf-8") + b"\n")
                await writer.drain()
                answer = ""
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.clients.discard(task)

    async def _discard_stderr(self):
        # OpenSSH may include remote-controlled banners/prompts/paths. Drain it
        # without retaining or rendering it; report only fixed application text.
        while await self.process.stderr.read(4096):
            pass

    async def connect(self):
        try:
            async with asyncio.timeout(SYSTEM_SFTP_LOGIN_SECONDS):
                self.server = await asyncio.start_server(self._respond, "127.0.0.1", 0, limit=8192)
                port = self.server.sockets[0].getsockname()[1]
                self.directory = tempfile.TemporaryDirectory(prefix="termana-system-sftp-")
                helper = Path(self.directory.name) / "response"
                script = Path(__file__).with_name("system_sftp_askpass.py").resolve()
                if any(c in str(sys.executable) + str(script) for c in '\r\n"\0'):
                    raise ValueError("Unsupported source runtime path")
                launcher = helper_command(
                    "system-sftp-response", ShellCommand(sys.executable, ("-I", str(script)))
                )
                response_argv = (launcher.program, *launcher.arguments)
                if os.name == "nt":
                    # Win32 OpenSSH supports an executable plus arguments in
                    # SSH_ASKPASS. Launch Python directly, never cmd.exe/.cmd:
                    # the server-controlled prompt remains an argv value.
                    askpass = subprocess.list2cmdline(response_argv)
                else:
                    text = f'#!/bin/sh\nexec {shlex.join(response_argv)} "$@"\n'
                    with helper.open("x", newline="") as stream:
                        stream.write(text)
                    helper.chmod(0o700)
                    askpass = str(helper)
                environment = {
                    name: value
                    for name, value in os.environ.items()
                    if not name.startswith(("TERMANA_", "SSH_ASKPASS", "PYTHON"))
                }
                environment.update(
                    {
                        "SSH_ASKPASS": askpass,
                        "SSH_ASKPASS_REQUIRE": "force",
                        CAPABILITY_ENV: json.dumps({"port": port, "token": self.token}),
                    }
                )
                # Older OS OpenSSH builds consult DISPLAY even when an AskPass
                # helper is explicitly configured. It is a child-only trigger,
                # not an X11 connection or forwarding request.
                environment.setdefault("DISPLAY", "termana-response:0")
                options = (
                    {"start_new_session": True}
                    if os.name != "nt"
                    else {"creationflags": subprocess.CREATE_NO_WINDOW}
                )
                if os.name != "nt":
                    argv = self.argv
                else:
                    child = helper_command(
                        "system-sftp-child",
                        ShellCommand(
                            sys.executable,
                            (
                                "-I",
                                str(Path(__file__).with_name("system_sftp_child.py")),
                                *self.argv,
                            ),
                        ),
                        self.argv,
                    )
                    argv = (child.program, *child.arguments)
                self.process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                    cwd=self.cwd,
                    limit=65536,
                    **options,
                )
                self.stderr_task = asyncio.create_task(self._discard_stderr())
                self.client = await open_pipe_sftp(self.process, self.tasks)
                self.authenticated = True
                self.server.close()
                await self.server.wait_closed()
                self.token = ""
                return self
        except BaseException:
            await self.close()
            raise

    async def open_sftp(self):
        if self._closed or self.client is None or self.process.returncode is not None:
            import asyncssh

            raise asyncssh.SFTPNoConnection("The separate System SFTP connection ended")
        # There is only one subsystem on this process. Never reconnect/replay a
        # request after SftpRuntime retires a failed client.
        if self.process.stdin.is_closing():
            import asyncssh

            raise asyncssh.SFTPNoConnection("The separate System SFTP channel ended")
        return self.client

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self.token = ""
        # Stop OpenSSH before rejecting its response helper, so a cancelled
        # response cannot turn into an empty-password authentication attempt.
        self._terminate()
        if self.server:
            self.server.close()
        for task in tuple(self.clients):
            task.cancel()
        await asyncio.gather(*tuple(self.clients), return_exceptions=True)
        if self.server:
            await asyncio.wait_for(self.server.wait_closed(), SYSTEM_SFTP_CLOSE_SECONDS)
        if self.process and self.process.returncode is None:
            try:
                if os.name == "nt":
                    self.process.terminate()  # Wrapper death closes its kill-on-close job.
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
                await asyncio.wait_for(self.process.wait(), SYSTEM_SFTP_CLOSE_SECONDS)
            except (ProcessLookupError, TimeoutError):
                if self.process.returncode is None:
                    if os.name == "nt":
                        self.process.kill()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
        await self.tasks.close()
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        if self.directory:
            self.directory.cleanup()

    def _terminate(self):
        if self.process and self.process.returncode is None:
            try:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
