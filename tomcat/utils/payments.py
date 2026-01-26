"""Provider detection shared between dues and finance email classifiers."""

from __future__ import annotations

from email.utils import parseaddr
from typing import Optional


def _extract_domain(from_addr: str) -> str:
    """Parse From header and extract lowercase domain from the email address."""
    _, email_part = parseaddr(from_addr or "")
    email_lower = (email_part or "").strip().lower()
    if "@" in email_lower:
        return email_lower.rsplit("@", 1)[1]
    return ""


def _domain_matches(domain: str, targets: tuple[str, ...]) -> bool:
    """Check if domain equals or is a subdomain of any target.

    Uses equality or safe .endswith() with leading dot to prevent partial matches.
    For example, domain "evilsquare.com" won't match target "square.com".
    """
    if not domain:
        return False
    for target in targets:
        if domain == target:
            return True
        #Subdomain check: domain ends with ".target"
        if domain.endswith("." + target):
            return True
    return False


def detect_provider(from_addr: str, subject: str = "", body: str = "") -> Optional[str]:
    """Infer provider slug from typical payment notification emails.

    Uses parsed email domain for provider identification to avoid incomplete
    substring matching vulnerabilities (CodeQL: py/incomplete-url-substring-sanitization).
    """
    domain = _extract_domain(from_addr)
    s = (subject or "").lower()
    b = (body or "").lower()

    def _any(text: str, phrases: tuple[str, ...]) -> bool:
        return any(phrase in text for phrase in phrases)

    venmo_markers = (
        "paid you",
        "sent you",
        "you paid",
        "you sent",
        "payment to",
        "receipt from",
    )
    venmo_domains = ("venmo.com",)
    if _domain_matches(domain, venmo_domains):
        if _any(s, venmo_markers) or _any(b, venmo_markers):
            return "venmo"

    cashapp_domains = ("cash.app", "squareup.com", "square.com")
    cashapp_markers = (
        "paid you",
        "sent you",
        "you paid",
        "you sent",
        "you spent",
        "payment to",
        "receipt",
    )
    if _domain_matches(domain, cashapp_domains):
        if _any(s, cashapp_markers) or _any(b, cashapp_markers):
            return "cashapp"

    paypal_domains = ("paypal.com",)
    paypal_markers = (
        "you've got money",
        "sent you",
        "payment received",
        "paid you",
        "you sent",
        "payment to",
        "receipt",
    )
    if _domain_matches(domain, paypal_domains):
        if _any(s, paypal_markers) or _any(b, paypal_markers):
            return "paypal"
    return None
