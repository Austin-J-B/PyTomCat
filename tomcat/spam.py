import re
from typing import Optional

SPAM_PATTERNS = [
    re.compile(r"free\s+.*(mac\s*book|macbook|iphone|ps\s*5|playstation)\b", re.I),
    re.compile(r"tickets?\s+(?:to|for)\s+.+(concert|show|tour|event)", re.I),
    re.compile(r"\b(?:dm|pm|message|text)\s+me\b.*\b(interested|if interested)\b", re.I),
    re.compile(r"first\s*come\s*first\s*serve", re.I),
    re.compile(r"\bmail\s+me\b|\bemail\s+me\b", re.I),
]

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"\+?1?\s*(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}")

try:
    from rapidfuzz import fuzz as rf_fuzz
    def _fuzzy_hit(text: str, phrase: str, thresh: int=88) -> bool:
        try:
            return rf_fuzz.partial_ratio(text.lower(), phrase.lower()) >= thresh
        except Exception:
            return False
except Exception:
    def _fuzzy_hit(text: str, phrase: str, thresh: int=88) -> bool:
        return phrase.lower() in (text or "").lower()

_nlp_cached = None

def _nlp_predict_spam(settings, text: str) -> float:
    global _nlp_cached
    if _nlp_cached is None:
        try:
            from .nlp.model import NLPModel
            _nlp_cached = NLPModel.maybe_load(settings)
        except Exception:
            _nlp_cached = False
    if not _nlp_cached:
        return 0.0
    try:
        # zero-shot: higher prob => more likely spam
        return float(_nlp_cached.predict_spam(text))
    except Exception:
        return 0.0

def _is_trusted_member(message, settings) -> Optional[str]:
    try:
        member = getattr(message, 'author', None)
        if not member:
            return False
        # Account age gate
        if getattr(member, 'created_at', None) is not None:
            from datetime import datetime, timezone
            age_days = (datetime.now(timezone.utc) - member.created_at).days
            if age_days >= int(getattr(settings, 'spam_min_account_days', 30) or 30):
                return "trusted_age"
        # Trusted roles
        trusted_list = [s.lower() for s in (getattr(settings, 'trusted_role_names', []) or [])]
        for r in getattr(member, 'roles', []) or []:
            rname = str(getattr(r, 'name', '')).lower()
            if any(t in rname for t in trusted_list):
                return "trusted_role"
        return None
    except Exception:
        return False

def check_spam(message, settings) -> tuple[bool, str]:
    text = (getattr(message, 'content', None) or '').strip()
    if not text:
        return (False, "empty")
    trust = _is_trusted_member(message, settings)
    if trust:
        return (False, trust)
    # Strong indicators
    if EMAIL_RE.search(text) or PHONE_RE.search(text):
        # allow one weak signal to pass but with contact info treat as strong
        pass_score = 1
    else:
        pass_score = 0
    score = pass_score
    matched_rules = []
    for rx in SPAM_PATTERNS:
        if rx.search(text):
            score += 2
            matched_rules.append(rx.pattern)
    # fuzzy phrases
    fuzzy_phrases = [
        "tickets available", "4 tickets", "american airlines center",
        "dm me if interested", "message me if interested", "first come first serve",
        "free macbook", "giving out my macbook", "free iphone","at&t stadium", "ps5 charger",
    ]
    fuzzy_hits = []
    for ph in fuzzy_phrases:
        if _fuzzy_hit(text, ph, 86):
            score += 1
            fuzzy_hits.append(ph)
    # NLP backstop
    spam_prob = _nlp_predict_spam(settings, text)
    if spam_prob >= float(getattr(settings, 'spam_nlp_conf', 0.9)):
        score += 2
    if score >= 2:
        reason = "rules"
        if spam_prob >= float(getattr(settings, 'spam_nlp_conf', 0.9)):
            reason = "nlp"
        return (True, reason)
    return (False, "none")

def is_spam(text: str) -> bool:
    # Legacy check for any callers using plain text
    if not text:
        return False
    if EMAIL_RE.search(text) or PHONE_RE.search(text):
        return True
    return any(p.search(text or "") for p in SPAM_PATTERNS)
