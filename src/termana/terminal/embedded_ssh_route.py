"""An explicit single-hop route using public AsyncSSH tunnelling, not proxy exec."""

import asyncio
from uuid import uuid4

from termana.models.connection import ConnectionProfile
from termana.models.ssh_identity import SshAuthenticationMode, SshIdentityOptions
from termana.models.ssh_jump import SshJumpMode, SshJumpOptions


def gateway_profile(profile):
    route = profile.ssh_jump
    return ConnectionProfile(
        "SSH gateway",
        route.host,
        port=route.port or 22,
        username=route.username,
        ssh_jump=SshJumpOptions(SshJumpMode.DIRECT),
        ssh_options=profile.ssh_options,
        ssh_identity=SshIdentityOptions(authentication=SshAuthenticationMode.PASSWORD_ONLY),
    )


async def connect_jump_ssh(
    plan,
    session_id,
    password_provider,
    gateway_password_provider,
    arguments,
    *,
    gateway_host_key_provider=None,
    gateway_authentication_id=None,
    gateway_ready_callback=None,
    gateway_disconnect_callback=None,
):
    from termana.terminal.embedded_ssh import (
        EMBEDDED_LOGIN_TIMEOUT_SECONDS,
        EmbeddedSshError,
        EmbeddedSshFailure,
        EmbeddedSshPlan,
        _connect_single_embedded_ssh,
    )

    if not callable(gateway_password_provider):
        raise EmbeddedSshError(EmbeddedSshFailure.CONFIGURATION)
    gateway = None
    handed_off = False
    try:
        async with asyncio.timeout(EMBEDDED_LOGIN_TIMEOUT_SECONDS):
            gateway = await _connect_single_embedded_ssh(
                EmbeddedSshPlan(gateway_profile(plan.profile), plan.known_hosts),
                gateway_authentication_id or uuid4(),
                gateway_password_provider,
                host_key_provider=gateway_host_key_provider or arguments["host_key_provider"],
                disconnect_callback=gateway_disconnect_callback,
            )
            # No gateway password may become a destination save proof.
            if gateway_ready_callback is None:
                gateway.take_proven_password()
            else:
                gateway_ready_callback(gateway)
            target = await _connect_single_embedded_ssh(
                plan,
                session_id,
                password_provider,
                tunnel=gateway.tunnel_transport,
                **arguments,
            )
            target._gateway = gateway
            handed_off = True
            return target
    except TimeoutError:
        raise EmbeddedSshError(EmbeddedSshFailure.TIMEOUT) from None
    finally:
        if gateway is not None and not handed_off:
            await gateway.close()
