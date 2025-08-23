import re

SPAM_PATTERNS = [
    re.compile(r"free\s+.*macbook", re.I),
    re.compile(r"tickets?.*\bto\b.*(concert|tour|event)", re.I),
    re.compile(r"(?:^|\s)(?:dm|pm)\s+me\s+.*\binterested\b", re.I),
    re.compile(r"first\s*come\s*first\s*serve", re.I),
]

def is_spam(text: str) -> bool:
    return any(p.search(text or "") for p in SPAM_PATTERNS)
