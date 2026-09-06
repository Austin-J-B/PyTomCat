"""Pure helpers for web/auth trust-boundary validation."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Collection
from urllib.parse import urlsplit


_OAUTH_MESSAGE_RE = re.compile(
    r"\b(?:auth|oauth)\s+(?:url|code)\b|[?&]code=[^\s&]+",
    re.IGNORECASE,
)


def origin_is_allowed(origin: str | None, allowed_origins: Collection[str]) -> bool:
    """Return true for absent origins or explicitly trusted browser origins."""
    if not origin:
        return True
    # Credentialed browser endpoints require exact origins. Treating "*" as a
    # wildcard risks turning a configuration shortcut into an auth bypass.
    return origin in allowed_origins and origin != "*"


def oauth_redirect_is_allowed(
    redirect_uri: str,
    allowed_origins: Collection[str],
    exact_redirect_uris: Collection[str],
) -> bool:
    """Constrain Discord redirects to configured UI origins or exact URIs."""
    raw = str(redirect_uri or "").strip()
    if not raw:
        return True  # Discord Activity codes do not send a redirect URI.
    if exact_redirect_uris:
        return raw in exact_redirect_uris
    try:
        parsed = urlsplit(raw)
    except Exception:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin_is_allowed(origin, allowed_origins)


def trusted_client_ip(remote: str | None, cf_connecting_ip: str | None) -> str:
    """Use Cloudflare's client IP only for a loopback origin connection."""
    remote_text = str(remote or "").strip()
    try:
        remote_ip = ipaddress.ip_address(remote_text)
    except ValueError:
        remote_ip = None
    if remote_ip is not None and remote_ip.is_loopback:
        candidate = str(cf_connecting_ip or "").strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return remote_text or "unknown"


def redact_oauth_message(text: str | None) -> str:
    """Keep short-lived OAuth codes and callback URLs out of durable logs."""
    raw = str(text or "")
    if _OAUTH_MESSAGE_RE.search(raw):
        return "[REDACTED: OAuth callback]"
    return raw
