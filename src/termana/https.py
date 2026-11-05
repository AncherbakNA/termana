"""OS-verified HTTPS for GitHub; never patch process-wide SSL behavior.

Explicit handlers leave SSH, RDP and independently pinned lab TLS untouched.
Failure to load native trust fails closed, never falls back to unverified TLS.
"""

import ssl
from urllib.request import HTTPSHandler

import truststore


def system_https_handler() -> HTTPSHandler:
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # PROTOCOL_TLS_CLIENT sets both; keep the contract explicit and local.
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return HTTPSHandler(context=context)
