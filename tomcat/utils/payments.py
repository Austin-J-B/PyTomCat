"""Provider detection shared between dues and finance email classifiers."""

from __future__ import annotations

import re
from email.utils import parseaddr
from typing import Optional


def extract_email_domain(from_addr: str) -> str:
    """Parse From header and extract lowercase domain from the email address."""
    _, email_part = parseaddr(from_addr or "")
    email_lower = (email_part or "").strip().lower()
    if "@" in email_lower:
        return email_lower.rsplit("@", 1)[1]
    return ""


def domain_matches(domain: str, targets: tuple[str, ...]) -> bool:
    """Check if domain equals or is a subdomain of any target.

    Uses equality or safe .endswith() with leading dot to prevent partial matches.
    For example, domain "evilsquare.com" won't match target "square.com", and
    "cash.app.attacker.com" won't match target "cash.app".
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


# Backward-compat aliases. New callers should use the names above.
_extract_domain = extract_email_domain
_domain_matches = domain_matches


#PayPal payment notifications carry no name in the subject -- every one of them
#reads "You've got money". The payer is only in the body. Reading the subject
#instead yields the literal string "You've", which is how four $15 dues payments
#ended up in the books as anonymous donations.
_PAYPAL_BODY_SENT = re.compile(
    r"^[^\S\n]*([A-Z][A-Za-z'`\-]+(?:[^\S\n]+[A-Z][A-Za-z'`\-]*\.?){0,3})"
    r"[^\S\n]+sent you[^\S\n]+\$?[\d,.]+",
    re.M,
)
_PAYPAL_NOTE_FROM = re.compile(r"Note from\s+([^\n<:]+)")
_PAYPAL_MONEY_FROM = re.compile(r"Money received\s+from\s+([^\n]+?)\s+\$[\d,.]+")

#Words a subject-line fallback produces when it finds no real name. Treating
#these as payers silently corrupts payment records, so they are rejected.
_NON_NAMES = {
    "you", "youve", "your", "money", "payment", "notification",
    "receipt", "paypal", "hello", "hi", "thanks",
}


def _looks_like_name(candidate: str) -> bool:
    """Reject subject-line noise that is not a person's name."""
    cleaned = (candidate or "").strip().strip(".:,").strip()
    if len(cleaned) < 2:
        return False
    squashed = re.sub(r"[^a-z]", "", cleaned.lower())
    return bool(squashed) and squashed not in _NON_NAMES


def extract_paypal_payer(subject: str = "", body: str = "") -> Optional[str]:
    """Return the payer name from a PayPal 'You've got money' notification.

    Body first, because that is the only place the name reliably appears.
    """
    for pattern in (_PAYPAL_BODY_SENT, _PAYPAL_NOTE_FROM, _PAYPAL_MONEY_FROM):
        m = pattern.search(body or "")
        if m:
            candidate = re.sub(r"\s+", " ", m.group(1)).strip().strip(".:,").strip()
            if _looks_like_name(candidate):
                return candidate
    #Only trust the subject when it actually names someone, as in
    #"Jane Doe sent you $15.00 USD".
    m = re.search(r"\b([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})\b\s+sent\s+you",
                  subject or "")
    if m and _looks_like_name(m.group(1)):
        return m.group(1).strip()
    return None


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
        "you were sent",
        "you paid",
        "you sent",
        "you spent",
        "payment received",
        "payment sent",
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
