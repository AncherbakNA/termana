"""Read only public AsyncSSH negotiation metadata on the authenticated worker."""

from termana.models.ssh_negotiation import (
    SshNegotiatedFacts,
    bounded_ssh_algorithm,
    bounded_ssh_fingerprint,
)

_DIRECTIONAL_FIELDS = (
    "send_cipher",
    "recv_cipher",
    "send_mac",
    "recv_mac",
    "send_compression",
    "recv_compression",
)


def snapshot_ssh_negotiation(connection) -> SshNegotiatedFacts:
    """Caller must prove exact live authentication; never inspect output or secrets.

    AsyncSSH 2.24.1's public get_extra_info documents these six keys; on a
    client, send means client-to-server and recv means server-to-client.
    get_server_host_key returns the key used in key exchange (or None for GSS).
    The key's algorithm is its key format, not an inferred signature algorithm.
    Missing/unsupported metadata never changes authentication or transport policy.
    """
    values = {}
    for name in _DIRECTIONAL_FIELDS:
        try:
            values[name] = bounded_ssh_algorithm(connection.get_extra_info(name))
        except Exception:
            values[name] = None
    algorithm, fingerprint = None, None
    try:
        key = connection.get_server_host_key()
        if key is not None:
            algorithm = bounded_ssh_algorithm(key.get_algorithm())
            fingerprint = bounded_ssh_fingerprint(key.get_fingerprint("sha256"))
    except Exception:
        pass  # No raw banner/error/payload is retained or logged.
    return SshNegotiatedFacts(
        **values, server_key_algorithm=algorithm, server_key_fingerprint=fingerprint
    )
