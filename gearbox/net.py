from __future__ import annotations

import ssl

import truststore


def ssl_context() -> ssl.SSLContext:
    """Verify TLS against the OS trust store, like Claude Code does.

    Antivirus HTTPS scanning (Kaspersky, ESET, ...) and corporate proxies re-sign traffic with a
    root CA that is trusted by Windows but missing from certifi's bundle.
    """
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
