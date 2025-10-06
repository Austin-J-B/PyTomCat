"""Provider detection shared between dues and finance email classifiers."""

from __future__ import annotations

from typing import Optional


def detect_provider(from_addr: str, subject: str = "", body: str = "") -> Optional[str]:
    """Heuristically determine which payment provider sent an email."""
    """Infer provider slug from typical payment notification emails."""
    f = (from_addr or "").lower()
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
    if "venmo.com" in f:
        if _any(s, venmo_markers) or _any(b, venmo_markers):
            return "venmo"

    cashapp_domains = ("cash.app", "squareup", "square.com", "cashapp")
    cashapp_markers = (
        "paid you",
        "sent you",
        "you paid",
        "you sent",
        "you spent",
        "payment to",
        "receipt",
    )
    if any(domain in f for domain in cashapp_domains):
        if _any(s, cashapp_markers) or _any(b, cashapp_markers):
            return "cashapp"

    paypal_markers = (
        "you've got money",
        "sent you",
        "payment received",
        "paid you",
        "you sent",
        "payment to",
        "receipt",
    )
    if "paypal.com" in f or "service@paypal" in f:
        if _any(s, paypal_markers) or _any(b, paypal_markers):
            return "paypal"
    return None
