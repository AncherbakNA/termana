"""Local capabilities, not server negotiation or a recommended policy."""

import subprocess
import sys
from importlib import import_module

from termana.models.settings import SshEngine
from termana.models.ssh_algorithms import SshAlgorithms
from termana.models.ssh_cipher import validate_ssh_cipher
from termana.terminal.ssh_ciphers import discover_ssh_ciphers
from termana.terminal.ssh_command import resolve_system_ssh_program


def validate_algorithm_list(result: object) -> tuple[str, ...]:
    if type(result) is not tuple or not 1 <= len(result) <= 512:
        raise ValueError("Invalid SSH capability list")
    for name in result:
        SshAlgorithms(kex=(name,))
    return tuple(dict.fromkeys(result))


def discover_engine_ciphers(engine: SshEngine) -> tuple[str, ...]:
    if type(engine) is not SshEngine:
        raise ValueError("Invalid SSH engine")
    if engine is SshEngine.SYSTEM:
        return discover_ssh_ciphers()
    # Import the package before any child modules. The three capability workers
    # can start together; importing different children of a cold AsyncSSH package
    # creates a circular import-lock graph (encryption -> connection -> kex).
    # Python's parent-module lock serializes initialization without blocking Qt.
    import_module("asyncssh")
    from asyncssh.encryption import get_encryption_algs

    names = tuple(n.decode("ascii") for n in get_encryption_algs())
    for name in names:
        validate_ssh_cipher(name)
    return names


def discover_ssh_algorithms(kind: str, engine: SshEngine) -> tuple[str, ...]:
    """Run off the GUI thread. Never read SSH config or connect to a server."""
    if kind not in ("kex", "host_keys") or type(engine) is not SshEngine:
        raise ValueError("Invalid SSH capability query")
    if engine is SshEngine.EMBEDDED:
        # Read installed library registries, not an unrelated OpenSSH binary.
        import_module("asyncssh")
        from asyncssh.kex import get_kex_algs
        from asyncssh.public_key import get_certificate_algs, get_public_key_algs

        names = get_kex_algs() if kind == "kex" else get_certificate_algs() + get_public_key_algs()
        return validate_algorithm_list(tuple(n.decode("ascii") for n in names))
    program = resolve_system_ssh_program()
    if program is None:
        raise RuntimeError("A trusted system SSH client is unavailable")
    def run(query):
        return subprocess.run(
            [program, "-Q", query],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
        )
    def parse(result):
        if result.returncode or type(result.stdout) is not bytes or len(result.stdout) > 65536:
            raise ValueError
        return validate_algorithm_list(tuple(result.stdout.decode("ascii").splitlines()))

    query = "kex" if kind == "kex" else "HostKeyAlgorithms"
    try:
        result = run(query)
        if (kind == "host_keys" and result.returncode == 255
                and type(result.stdout) is bytes and result.stdout == b""):
            # Built-in Windows OpenSSH predates the config-keyword query alias
            # (and key-sig). Query its actual key/signature registries instead;
            # no synthesized algorithms, server/config reads or policy changes.
            keys = parse(run("key"))
            signatures = parse(run("sig"))
            return validate_algorithm_list(tuple(dict.fromkeys((*keys, *signatures))))
        return parse(result)
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        raise RuntimeError("Could not read the local SSH capability list") from None
