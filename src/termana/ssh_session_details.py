"""Read-only requested settings and separately verified Embedded destination facts."""

from termana.i18n import tr
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.models.open_terminal import OpenTerminalState
from termana.models.ssh_jump import SshJumpMode
from termana.models.ssh_negotiation import SshNegotiatedFacts
from termana.models.ssh_session_status import SshSessionStatus

SSH_DETAILS_NOTICE = (
    "The launch settings below are profile options requested for this launch, not negotiated connection details. "
    "Later edits to the saved profile do not change this snapshot. "
    "Verified Embedded SSH facts, when available, are shown separately."
)
SSH_DETAILS_PRIVACY = (
    "Includes hostnames, usernames, local paths and any verified server-key fingerprint. "
    "Review before sharing."
)
SSH_DETAILS_REDACTED_PRIVACY = (
    "Connection names, addresses, logins, ports, key paths and server-key fingerprints are hidden. "
    "Requested options, rule count and process state remain visible. Review before sharing. "
    "This does not sanitize the terminal or clear a previous clipboard copy."
)
_INHERITED = "OpenSSH config / defaults (not inspected)"


def _endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _one_line(value: str) -> str:
    # Printable separators allowed in names/paths must not forge report fields.
    return value.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def ssh_session_details_text(
    profile: ConnectionProfile,
    *,
    redacted: bool = False,
    status: SshSessionStatus | None = None,
    negotiation: SshNegotiatedFacts | None = None,
) -> str:
    """Format only allowlisted model fields; no config, keys, commands, or output reads."""
    if type(profile) is not ConnectionProfile or profile.protocol is not ConnectionProtocol.SSH:
        raise ValueError("SSH session details require an SSH profile")
    if type(redacted) is not bool or (status is not None and type(status) is not SshSessionStatus):
        raise TypeError("Invalid SSH report presentation options")
    if negotiation is not None and type(negotiation) is not SshNegotiatedFacts:
        raise TypeError("Invalid SSH negotiation snapshot")
    if status is not None and status.state is OpenTerminalState.ENDED:
        negotiation = None
    route = profile.ssh_jump
    options = profile.ssh_options
    identity = profile.ssh_identity
    defaults = (
        "Legacy component defaults (no OpenSSH config)"
        if profile.ssh_algorithms.protocol == "1" else _INHERITED
    )
    route_text = (
        (
            tr("Jump host: [hidden]")
            if redacted
            else tr("Jump host: {p0}", p0=f"{route.proxy_jump_value}")
        )
        if route.mode is SshJumpMode.JUMP
        else tr("Direct (no proxy)")
        if route.mode is SshJumpMode.DIRECT
        else tr(defaults)
    )
    keepalive = (
        tr(defaults)
        if options.server_alive_interval is None
        else tr("Disabled; a silent connection failure may keep waiting")
        if options.server_alive_interval == 0
        else tr(
            "Every {p0} s; {p1} unanswered probes",
            p0=f"{options.server_alive_interval}",
            p1=f"{options.server_alive_count_max}",
        )
    )
    lines = [
        tr("SSH SESSION · LAUNCH SNAPSHOT"),
        tr(SSH_DETAILS_NOTICE),
        "",
        "DESTINATION",
        tr("Session: {p0}", p0=f"{('[hidden]' if redacted else _one_line(profile.name))}"),
        tr(
            "Host / alias: {p0}",
            p0=f"{('[hidden]' if redacted else _endpoint(profile.host, profile.port))}",
        ),
        tr("Requested login: ")
        + (tr("[hidden]") if redacted and profile.username else profile.username or tr(defaults)),
        tr("Route: {p0}", p0=f"{route_text}"),
        "",
        tr("CONNECTION OPTIONS"),
        tr("Requested protocol: ") + (
            tr("SSH1 only — insecure separate legacy component; no OpenSSH config, SFTP or forwarding")
            if profile.ssh_algorithms.protocol == "1"
            else tr("SSH2 (no automatic downgrade)")
        ),
        tr("Requested cipher: {p0}", p0=f"{profile.ssh_cipher or tr(defaults)}"),
        tr("Handshake timeout: ")
        + (
            tr(defaults)
            if options.connect_timeout is None
            else tr("{p0} s", p0=f"{options.connect_timeout}")
        ),
        tr("SSH keepalive: {p0}", p0=f"{keepalive}"),
        "",
        tr("CONFIGURED IDENTITY OPTIONS"),
        tr("Authentication mode: {p0}", p0=f"{identity.authentication.value}"),
        tr("Additional identity file: ")
        + (
            tr("Configured [path hidden]")
            if redacted and identity.identity_file
            else _one_line(identity.identity_file or tr(defaults))
        ),
        tr("IdentitiesOnly: ")
        + (
            tr(defaults)
            if identity.identities_only is None
            else "yes"
            if identity.identities_only
            else "no"
        ),
        tr("Agent use: {p0}", p0=f"{('Disabled' if identity.agent_disabled or profile.ssh_algorithms.protocol == '1' else tr(defaults))}"),
        tr(
            "These settings do not identify the key actually used. Choosing a saved password can bypass key authentication. No key contents or saved passwords are shown."
        ),
        "",
        tr("LOCAL TUNNEL RULES"),
    ]
    if profile.ssh_tunnels.local_forwards:
        lines.extend(
            tr("Rule {p0}: loopback [port hidden] → [destination and port hidden]", p0=f"{index}")
            if redacted
            else f"127.0.0.1:{rule.local_port} → {_endpoint(rule.destination_host, rule.destination_port)}"
            for index, rule in enumerate(profile.ssh_tunnels.local_forwards, 1)
        )
        lines.append(
            tr(
                "Destinations are reached from the final SSH server. These are requested rules, not listener status or proof that a destination service is reachable. Rules add to SSH-config forwards and can re-enable inherited forwarding, including non-loopback listeners."
            )
        )
    else:
        lines.append(tr("No Termana rules. Forwarding inherited from SSH config is not inspected."))
    lines.extend(("", tr("VERIFIED EMBEDDED SSH · DESTINATION")))
    if negotiation is None:
        lines.append(
            tr(
                "Unavailable: no live authenticated Embedded SSH snapshot. System OpenSSH negotiation is not inspected."
            )
        )
    else:
        lines.append(
            tr(
                "Authentication-complete negotiation snapshot from this destination connection, not the gateway. Algorithms can change after rekey; this is not live rekey status."
            )
        )
        lines.extend(
            f"{label}: {getattr(negotiation, field) or 'unavailable'}"
            for label, field in (
                (tr("Client → server cipher"), "send_cipher"),
                (tr("Server → client cipher"), "recv_cipher"),
                (tr("Client → server MAC"), "send_mac"),
                (tr("Server → client MAC"), "recv_mac"),
                (tr("Client → server compression"), "send_compression"),
                (tr("Server → client compression"), "recv_compression"),
                (tr("Verified server-key algorithm (key format)"), "server_key_algorithm"),
            )
        )
        lines.append(
            tr("Verified server-key SHA256 fingerprint: ")
            + (
                tr("[hidden]")
                if redacted and negotiation.server_key_fingerprint
                else negotiation.server_key_fingerprint or tr("unavailable")
            )
        )
    lines.extend(
        (
            "",
            "SCOPE",
            tr(
                "No resolved address, authentication method or gateway configuration is inferred here. Process state alone is not an authentication result or proof of server health. Host verification remains the selected SSH engine's job. Viewing or copying this snapshot does not connect, approve tunnels, or change settings."
            ),
            tr(SSH_DETAILS_REDACTED_PRIVACY) if redacted else tr(SSH_DETAILS_PRIVACY),
        )
    )
    if status is not None:
        lines.extend(
            (
                "",
                tr("TERMINAL PROCESS STATE"),
                tr("State: {p0}", p0=tr(status.state.value, context="terminal-state")),
                tr("Exit code: {p0}", p0=f"{status.exit_code}")
                if status.exit_code is not None
                else tr("Exit code: not available"),
                tr(
                    "Application process state is not proof of successful SSH authentication or server health. This report does not read password input or remote terminal output."
                ),
            )
        )
    return "\n".join(lines)
