"""Provider detection shared between dues and finance email classifiers."""

from __future__ import annotations

from typing import Optional


def detect_provider(from_addr: str, subject: str = "", body: str = "") -> Optional[str]:
    """Heuristically determine which payment provider sent an email."""
    """Infer provider slug from typical payment notification emails."""
    f = (from_addr or "").lower()
    s = (subject or "").lower()
    b = (body or "").lower()
    if "venmo.com" in f:
        if ("paid you" in s) or ("sent you" in s) or ("paid you" in b):
            return "venmo"
    if any(domain in f for domain in ("cash.app", "squareup", "square.com", "cashapp")):
        if ("paid you" in s) or ("sent you" in s) or ("paid you" in b):
            return "cashapp"
    if "paypal.com" in f or "service@paypal" in f:
        if ("you've got money" in s) or ("sent you" in s) or ("payment received" in s) or ("paid you" in b):
            return "paypal"
    return None
