"""Explicit bounded encrypted loopback diagnostic, including frozen helper paths."""

import asyncio
import json
import secrets
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


async def check_transports(root: Path) -> dict:
    import asyncssh

    from termana.models.connection import ConnectionProfile
    from termana.models.ssh_identity import SshAuthenticationMode, SshIdentityOptions
    from termana.models.ssh_jump import SshJumpMode, SshJumpOptions
    from termana.terminal.embedded_ssh import (
        EmbeddedSshError,
        EmbeddedSshFailure,
        EmbeddedSshPlan,
        connect_embedded_ssh,
    )
    from termana.terminal.ssh_command import resolve_system_ssh_program
    from termana.terminal.system_sftp_connection import SystemSftpConnection

    key = asyncssh.generate_private_key("ssh-ed25519")
    password = secrets.token_urlsafe(32)
    connections, attempts = set(), []
    remote = root / "remote"
    remote.mkdir(mode=0o700)

    class Server(asyncssh.SSHServer):
        def connection_made(self, connection):
            self.connection = connection
            connections.add(connection)

        def connection_lost(self, _error):
            connections.discard(self.connection)

        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, supplied):
            accepted = username == "runtime_fixture" and supplied == password
            attempts.append(accepted)
            return accepted

    async def echo(process):
        try:
            process.stdout.write(b"RUNTIME_READY\r\n")
            while data := await process.stdin.read(4096):
                process.stdout.write(data)
        except (asyncssh.Error, OSError):
            pass
        finally:
            process.exit(0)

    server = await asyncssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[key],
        encoding=None,
        process_factory=echo,
        sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(remote)),
    )
    owner = separate = None
    try:
        port = server.get_port()
        trust = root / "known-hosts"
        trust.write_bytes(f"[127.0.0.1]:{port} ".encode() + key.export_public_key())
        trust.chmod(0o600)
        profile = ConnectionProfile(
            "Runtime loopback",
            "127.0.0.1",
            port=port,
            username="runtime_fixture",
            ssh_jump=SshJumpOptions(SshJumpMode.DIRECT),
            ssh_identity=SshIdentityOptions(authentication=SshAuthenticationMode.PASSWORD_ONLY),
        )

        async def provide():
            return password

        owner = await connect_embedded_ssh(EmbeddedSshPlan(profile, trust), uuid4(), provide)
        terminal = await owner.open_terminal(80, 24)
        assert await terminal.stdout.readexactly(15) == b"RUNTIME_READY\r\n"
        marker = b"Az09_-+=/\\.:,;@[]{}()~!|<>?\r"
        terminal.stdin.write(marker)
        assert await terminal.stdout.readexactly(len(marker)) == marker
        sftp = await owner.open_sftp()
        payload = bytes(range(256)) * 64
        async with sftp.open("probe Ω.bin", "wb") as stream:
            await stream.write(payload)
        async with sftp.open("probe Ω.bin", "rb") as stream:
            assert await stream.read() == payload
        assert attempts == [True] and owner.take_proven_password() is not None
        sftp.exit()
        await sftp.wait_closed()
        # Wrong host key must fail before requesting credentials, even in a bundle.
        wrong = root / "wrong-hosts"
        wrong.write_bytes(
            f"[127.0.0.1]:{port} ".encode()
            + asyncssh.generate_private_key("ssh-ed25519").export_public_key()
        )
        wrong.chmod(0o600)
        asked = []

        async def forbidden():
            asked.append(True)
            return password

        try:
            unexpected = await connect_embedded_ssh(
                EmbeddedSshPlan(profile, wrong), uuid4(), forbidden
            )
        except EmbeddedSshError as error:
            assert error.failure is EmbeddedSshFailure.HOST_KEY and not asked
        else:
            await unexpected.close()
            raise RuntimeError("Wrong-key check did not refuse the peer")
        program = resolve_system_ssh_program()
        assert program is not None
        config = root / "ssh-config"
        config.write_text(
            "Host *\n UserKnownHostsFile "
            + json.dumps(str(trust))
            + "\n GlobalKnownHostsFile none\n IdentityAgent none\n ConnectTimeout 3\n"
        )

        async def response(_prompt):
            return password

        separate = SystemSftpConnection(profile, program, response)
        separate.argv = (program, "-F", str(config), *separate.argv[1:])
        await separate.connect()
        files = await separate.open_sftp()
        async with files.open("probe Ω.bin", "rb") as stream:
            assert await stream.read() == payload
        assert attempts == [True, True]
        return dict(
            embedded_pty=True,
            embedded_sftp=True,
            separate_system_sftp=True,
            wrong_key_before_credentials=True,
            exact_binary_bytes=len(payload),
            authentication_count=2,
        )
    finally:
        if separate is not None:
            await separate.close()
        if owner is not None:
            await owner.close()
        for connection in tuple(connections):
            connection.close()
        await asyncio.gather(*(c.wait_closed() for c in tuple(connections)), return_exceptions=True)
        server.close()
        await server.wait_closed()


def main(report_path: str) -> int:
    try:
        stream = Path(report_path).open("x", encoding="utf-8")
    except OSError:
        return 2
    result = dict(
        passed=False,
        remote_connections=False,
        loopback_only=True,
        user_data_accessed=False,
        platform=sys.platform,
    )
    with stream:
        try:
            with tempfile.TemporaryDirectory(prefix="termana-transport-check-") as directory:

                async def bounded():
                    async with asyncio.timeout(40):
                        return await check_transports(Path(directory))

                result.update(asyncio.run(bounded()))
                result["passed"] = True
        except Exception as error:
            result["errors"] = [f"Transport check failed ({type(error).__name__})."]
        json.dump(result, stream, indent=2)
    return 0 if result["passed"] else 1
