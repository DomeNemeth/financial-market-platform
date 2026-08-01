"""
TLS trust configuration.

Python's `ssl` module verifies certificates against the `certifi` CA bundle,
which knows nothing about roots installed in the operating system's trust
store. That breaks any environment where TLS is intercepted by a local proxy
or antivirus — the interceptor's root CA is installed in the OS store, so
browsers and OS-aware tools work fine while `requests` fails with
"unable to get local issuer certificate".

`truststore` redirects verification to the OS trust store. Certificate
verification stays fully enabled — we are changing *which* set of roots is
trusted, not whether trust is checked. Never substitute `verify=False`.

Call `enable_system_trust_store()` once at the start of any entrypoint that
makes outbound HTTPS requests. It is process-global and idempotent.
"""

import logging

logger = logging.getLogger(__name__)

_injected = False


def enable_system_trust_store() -> bool:
    """
    Route TLS verification through the OS trust store instead of certifi.

    Returns True if injection is active. Safe to call more than once, and
    safe where truststore is unavailable — it degrades to certifi rather
    than failing the process.
    """
    global _injected
    if _injected:
        return True

    try:
        import truststore
    except ImportError:
        logger.debug("truststore not installed — falling back to the certifi CA bundle")
        return False

    truststore.inject_into_ssl()
    _injected = True
    logger.debug("TLS verification now uses the OS trust store")
    return True
