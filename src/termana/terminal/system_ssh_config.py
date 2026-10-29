"""Reviewed, inert gateway imports and private immutable OpenSSH configurations.

No user/system config is included or evaluated by OpenSSH. Keys remain references,
never copied. A basename -F plus an explicit child cwd avoids the unquoted path
which native ProxyJump otherwise places inside its generated proxy command.
"""

import fnmatch
import hashlib
import json
import os
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from termana.models.ssh_jump import SshJumpMode, SshJumpOptions, validate_ssh_route_host
from termana.models.ssh_options import SshConnectionOptions
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.terminal.private_recovery_files import PrivateDirectory, PrivateFile
from termana.terminal.sftp_local_files import open_sftp_source
from termana.terminal.ssh_command import ssh_known_hosts_arguments

MAX_BYTES, MAX_GATEWAYS, MAX_SNAPSHOTS = 64 * 1024, 128, 64
CONFIG_NAME = "ssh_config"
HEADER = "# Termana managed System SSH config v2\n# Gateways: "
LEGACY_HEADER = "# Termana managed System SSH config v1\n# Gateways: "
_ALLOWED = frozenset(
    (
        "hostname",
        "port",
        "user",
        "identityfile",
        "identitiesonly",
        "identityagent",
        "connecttimeout",
        "serveraliveinterval",
        "serveralivecountmax",
        "proxyjump",
    )
)


class SystemSshConfigError(ValueError):
    """A fixed user-facing refusal, without untrusted source text or secrets."""


def _refused():
    return SystemSshConfigError(
        "Gateway config refused. Supported: Host/HostName/Port/User, IdentityFile, IdentitiesOnly, IdentityAgent and connection timers. No Include, Match, commands, nested gateways, trust overrides or expansion tokens. No source files or keys were changed."
    )


def _path(value):
    if type(value) is not str or any(c in value for c in ("\0", "\r", "\n", "%", "$", "`")):
        raise _refused()
    if value.startswith("~/"):
        value = str(Path.home() / value[2:])
    path = Path(value)
    if not path.is_absolute():
        raise _refused()
    return str(path)


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    alias: str
    host: str
    port: int = 22
    username: str | None = None
    identity_files: tuple[str, ...] = ()
    identities_only: bool | None = None
    identity_agent: str | None = None
    connect_timeout: int | None = None
    server_alive_interval: int | None = None
    server_alive_count_max: int | None = None

    def __post_init__(self):
        validate_ssh_route_host(self.alias)
        if self.alias.casefold() == "none" or type(self.port) is not int:
            raise _refused()
        route = SshJumpOptions(SshJumpMode.JUMP, self.host, self.port, self.username)
        if route.host != self.host or route.username != self.username:
            raise _refused()
        if type(self.identity_files) is not tuple or len(self.identity_files) > 8:
            raise _refused()
        if any(_path(value) != value for value in self.identity_files):
            raise _refused()
        if self.identities_only is not None and type(self.identities_only) is not bool:
            raise _refused()
        if (
            self.identity_agent not in (None, "none")
            and _path(self.identity_agent) != self.identity_agent
        ):
            raise _refused()
        SshConnectionOptions(
            self.connect_timeout, self.server_alive_interval, self.server_alive_count_max
        )


def import_gateway(path: Path, alias: str) -> GatewaySettings:
    """Project one selected alias; no subprocess, Include, key or trust reads."""
    validate_ssh_route_host(alias)
    with open_sftp_source(path) as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise _refused()
    try:
        lines = raw.decode("utf-8-sig").splitlines()
        if len(lines) > 2048:
            raise _refused()
        values, identities, active, matched = {}, [], True, False
        for line in lines:
            if "\0" in line or len(line) > 4096:
                raise _refused()
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            found = re.fullmatch(r"([A-Za-z]+)(?:\s*=\s*|\s+)(.+)", line)
            if found is None:
                raise _refused()
            keyword, value = found[1].lower(), found[2]
            lexer = shlex.shlex(value, posix=True)
            lexer.whitespace_split, lexer.escape = True, ""
            tokens = list(lexer)
            if not tokens or any(any(c in t for c in "%$`\r\n\0") for t in tokens):
                raise _refused()
            if keyword == "host":
                if any(re.fullmatch(r"!?[A-Za-z0-9_.:*?-]+", t) is None for t in tokens):
                    raise _refused()
                negative = any(
                    t.startswith("!") and fnmatch.fnmatchcase(alias.lower(), t[1:].lower())
                    for t in tokens
                )
                active = not negative and any(
                    not t.startswith("!") and fnmatch.fnmatchcase(alias.lower(), t.lower())
                    for t in tokens
                )
                matched |= active
                continue
            if keyword not in _ALLOWED or len(tokens) != 1:
                raise _refused()
            if active:
                if keyword == "identityfile":
                    identities.append(_path(tokens[0]))
                else:
                    values.setdefault(keyword, tokens[0])
        if not matched or values.get("proxyjump", "none").lower() != "none":
            raise _refused()
        def integer(name, default=None):
            return int(values[name]) if name in values else default
        only = values.get("identitiesonly")
        if only is not None and only.lower() not in ("yes", "no"):
            raise _refused()
        agent = values.get("identityagent")
        interval = integer("serveraliveinterval")
        count = integer("serveralivecountmax", 3 if interval is not None and interval > 0 else None)
        return GatewaySettings(
            alias,
            values.get("hostname", alias),
            integer("port", 22),
            values.get("user"),
            tuple(identities),
            None if only is None else only.lower() == "yes",
            None if agent is None else "none" if agent.lower() == "none" else _path(agent),
            integer("connecttimeout"),
            interval,
            count,
        )
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise _refused() from None


def _quote(path):
    return ssh_known_hosts_arguments(Path(path))[1].partition("=")[2]


def render_config(gateways, trust, *, legacy=False):
    metadata = json.dumps([asdict(g) for g in gateways], sort_keys=True, separators=(",", ":"))
    lines = [
        (LEGACY_HEADER if legacy else HEADER) + metadata,
        "Host *",
        "    UserKnownHostsFile " + (_quote(trust) if legacy else '"../known_hosts"'),
        "    GlobalKnownHostsFile none",
        "    StrictHostKeyChecking ask",
        "    UpdateHostKeys no",
        "    ControlMaster no",
        "    ControlPersist no",
        "    ControlPath none",
        "    ForwardAgent no",
        "    ForwardX11 no",
        "    PermitLocalCommand no",
        "    ProxyJump none",
    ]
    for g in gateways:
        lines.extend(("", "Host " + g.alias, "    HostName " + g.host, "    Port " + str(g.port)))
        if g.username is not None:
            lines.append("    User " + g.username)
        for identity in g.identity_files:
            lines.append("    IdentityFile " + _quote(identity))
        for key, value in (
            (
                "IdentitiesOnly",
                None if g.identities_only is None else "yes" if g.identities_only else "no",
            ),
            (
                "IdentityAgent",
                None
                if g.identity_agent is None
                else "none"
                if g.identity_agent == "none"
                else _quote(g.identity_agent),
            ),
            ("ConnectTimeout", g.connect_timeout),
            ("ServerAliveInterval", g.server_alive_interval),
            ("ServerAliveCountMax", g.server_alive_count_max),
        ):
            if value is not None:
                lines.append(f"    {key} {value}")
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES or len(gateways) > MAX_GATEWAYS:
        raise SystemSshConfigError("The Termana gateway config reached its size limit.")
    return raw


@dataclass(frozen=True, slots=True)
class GatewayCatalog:
    gateways: tuple[GatewaySettings, ...]
    revision: str | None


class SystemSshConfigStore:
    """Cold store; writes only on explicit import or an actual System launch."""

    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("System config root must be absolute")
        self.path, self.trust = self.root / CONFIG_NAME, self.root / "known_hosts"
        self._lock = RLock()

    def _read(self, directory):
        try:
            with PrivateFile(directory, CONFIG_NAME) as source:
                raw, status = source.read(MAX_BYTES), os.fstat(source.fd)
        except FileNotFoundError:
            return GatewayCatalog((), None), None, None
        try:
            if not raw.startswith((HEADER.encode(), LEGACY_HEADER.encode())):
                raise _refused()
            metadata = raw.split(b"\n", 2)[1].removeprefix(b"# Gateways: ")
            items = json.loads(metadata, object_pairs_hook=reject_duplicate_json_keys)
            if type(items) is not list or len(items) > MAX_GATEWAYS:
                raise _refused()
            gateways = []
            for item in items:
                if type(item) is not dict or type(item.get("identity_files")) is not list:
                    raise _refused()
                item["identity_files"] = tuple(item["identity_files"])
                gateways.append(GatewaySettings(**item))
            if (
                len({g.alias.casefold() for g in gateways}) != len(gateways)
                or raw not in (
                    render_config(gateways, self.trust),
                    render_config(gateways, self.trust, legacy=True),
                )
            ):
                raise _refused()
            return GatewayCatalog(tuple(gateways), hashlib.sha256(raw).hexdigest()), raw, status
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise SystemSshConfigError(
                "Termana SSH config changed or is unsupported. Original files are retained; review ssh_config before connecting."
            ) from None

    def load(self):
        with self._lock:
            if not self.root.exists():
                return GatewayCatalog((), None)
            with PrivateDirectory(self.root) as directory:
                return self._read(directory)[0]

    def _snapshot(self, directory, raw):
        root = self.root / "ssh-configs"
        with PrivateDirectory(
            root, parent=directory, create=not root.exists(), writable=True
        ) as snapshots:
            name = "ssh-" + hashlib.sha256(raw).hexdigest() + ".conf"
            if name not in snapshots.names(maximum=MAX_SNAPSHOTS):
                if len(snapshots.names(maximum=MAX_SNAPSHOTS)) >= MAX_SNAPSHOTS:
                    raise SystemSshConfigError(
                        "SSH config snapshot limit reached. Existing snapshots were retained; review them before importing more gateways."
                    )
                with PrivateFile(snapshots, name, create=True) as target:
                    target.rewrite(raw)
            with PrivateFile(snapshots, name) as target:
                if target.read(MAX_BYTES) != raw:
                    raise _refused()
            return root / name

    def _publish(self, directory, before, status, raw):
        if os.name == "nt":
            from termana.credentials.windows_private_files import commit_windows_trust

            commit_windows_trust(self.path, before or b"", status, raw, directory=directory)
            return
        name = ".ssh-config-" + uuid4().hex
        published = False
        with PrivateFile(directory, name, create=True) as stage:
            try:
                stage.rewrite(raw)
                if self._read(directory)[1] != before:
                    raise SystemSshConfigError(
                        "Termana SSH config changed during import; nothing was replaced."
                    )
                if before is None:
                    os.link(
                        name,
                        CONFIG_NAME,
                        src_dir_fd=directory.handle,
                        dst_dir_fd=directory.handle,
                        follow_symlinks=False,
                    )
                    os.unlink(name, dir_fd=directory.handle)
                else:
                    os.replace(
                        name, CONFIG_NAME, src_dir_fd=directory.handle, dst_dir_fd=directory.handle
                    )
                published = True
                directory.sync()
            finally:
                if not published:
                    stage.delete()

    def prepare(self):
        with (
            self._lock,
            PrivateDirectory(self.root, create=not self.root.exists(), writable=True) as directory,
        ):
            catalog, raw, status = self._read(directory)
            if raw is None:
                raw = render_config((), self.trust)
                self._publish(directory, None, status, raw)
            # Existing canonical v1 files are retained byte-for-byte. New child
            # processes get a v2 snapshot; active tabs keep their old revision.
            return self._snapshot(directory, render_config(catalog.gateways, self.trust))

    def prepare_sftp(self, original):
        """Reuse the owning tab's gateway revision; never ask SFTP to trust a key."""
        if not isinstance(original, Path) or original.parent != self.root / "ssh-configs":
            raise SystemSshConfigError(
                "The SSH tab has no owned config snapshot; reconnect it before opening SFTP."
            )
        with self._lock, PrivateDirectory(self.root, writable=True) as directory:
            with PrivateDirectory(original.parent, parent=directory) as snapshots:
                with PrivateFile(snapshots, original.name) as source:
                    raw = source.read(MAX_BYTES)
            if original.name != "ssh-" + hashlib.sha256(raw).hexdigest() + ".conf":
                raise SystemSshConfigError(
                    "The SSH config snapshot changed; reconnect before opening SFTP."
                )
            if raw.startswith(LEGACY_HEADER.encode()):
                old_trust = ("    UserKnownHostsFile " + _quote(self.trust) + "\n").encode()
                if raw.count(old_trust) != 1:
                    raise _refused()
                raw = raw.replace(LEGACY_HEADER.encode(), HEADER.encode(), 1).replace(
                    old_trust, b'    UserKnownHostsFile "../known_hosts"\n', 1
                )
            policy = b"    StrictHostKeyChecking ask\n"
            if raw.count(policy) != 1:
                raise _refused()
            return self._snapshot(
                directory,
                raw.replace(
                    policy, b"    StrictHostKeyChecking yes\n    ClearAllForwardings yes\n", 1
                ),
            )

    def save_gateway(self, gateway, *, expected_revision):
        if type(gateway) is not GatewaySettings:
            raise _refused()
        with (
            self._lock,
            PrivateDirectory(self.root, create=not self.root.exists(), writable=True) as directory,
        ):
            catalog, before, status = self._read(directory)
            if catalog.revision != expected_revision:
                raise SystemSshConfigError(
                    "Gateway settings changed after review. Import again; no current data was replaced."
                )
            gateways = tuple(
                sorted(
                    (
                        *[
                            g
                            for g in catalog.gateways
                            if g.alias.casefold() != gateway.alias.casefold()
                        ],
                        gateway,
                    ),
                    key=lambda g: g.alias.casefold(),
                )
            )
            raw = render_config(gateways, self.trust)
            if before is not None:
                self._snapshot(directory, before)  # Original retained before publication.
            self._snapshot(
                directory, raw
            )  # Immutable file for child processes, also validates limits.
            self._publish(directory, before, status, raw)
