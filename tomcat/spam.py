"""Spam heuristics, NLP backstop, and trusted-member exemptions."""

import re
from collections import defaultdict
from typing import Optional, Tuple

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
_MESSAGE_COUNTS: dict[Tuple[int, int], int] = defaultdict(int)


def _message_count_key(message) -> Tuple[int, int]:
    """Produce a (guild_id, user_id) tuple for per-user spam heuristics."""
    try:
        guild_id = int(getattr(getattr(message, 'guild', None), 'id', 0) or 0)
    except Exception:
        guild_id = 0
    try:
        user_id = int(getattr(getattr(message, 'author', None), 'id', 0) or 0)
    except Exception:
        user_id = 0
    return (guild_id, user_id)

def _nlp_predict_spam(settings, text: str) -> float:
    """Run the optional NLP model and return a spam probability."""
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

def _has_privileged_role(member, settings) -> Optional[str]:
    """Check for role-based exemptions and explain the trust reason."""
    try:
        roles = getattr(member, 'roles', []) or []
        trusted_list = [s.lower() for s in (getattr(settings, 'trusted_role_names', []) or [])]
        for r in roles:
            name = str(getattr(r, 'name', '')).lower()
            if any(token in name for token in trusted_list):
                return "trusted_role"
    except Exception:
        pass
    return None


def _is_trusted_member(message, settings, *, prior_count: int = 0) -> Optional[str]:
    """Apply role/account-age/message-count gates before running spam rules."""
    try:
        member = getattr(message, 'author', None)
        if not member:
            return False
        try:
            if getattr(member, 'guild_permissions', None):
                perms = member.guild_permissions
                if getattr(perms, 'administrator', False):
                    return "trusted_admin"
                if getattr(perms, 'manage_guild', False) or getattr(perms, 'ban_members', False):
                    return "trusted_moderator"
        except Exception:
            pass
        admin_ids = {int(x) for x in (getattr(settings, 'admin_ids', []) or [])}
        try:
            if admin_ids and int(getattr(member, 'id', 0)) in admin_ids:
                return "trusted_admin"
        except Exception:
            pass
        msg_threshold = int(getattr(settings, 'spam_trust_message_threshold', 50) or 50)
        if prior_count >= msg_threshold:
            return "trusted_message_count"
        # Account age gate
        if getattr(member, 'created_at', None) is not None:
            from datetime import datetime, timezone
            age_days = (datetime.now(timezone.utc) - member.created_at).days
            if age_days >= int(getattr(settings, 'spam_min_account_days', 30) or 30):
                return "trusted_age"
        # Trusted roles
        role_reason = _has_privileged_role(member, settings)
        if role_reason:
            return role_reason
        return None
    except Exception:
        return False

def check_spam(message, settings) -> tuple[bool, str]:
    """Main spam check: returns (is_spam, reason)."""
    text = (getattr(message, 'content', None) or '').strip()
    if not text:
        return (False, "empty")
    key = _message_count_key(message)
    prior_count = _MESSAGE_COUNTS.get(key, 0)
    _MESSAGE_COUNTS[key] = prior_count + 1
    trust = _is_trusted_member(message, settings, prior_count=prior_count)
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
