"""Spam heuristics and trusted-member exemptions."""

import re
import time
from collections import defaultdict
from typing import Optional, Tuple

from .logger import log_action
from .utils.permissions import is_officer

SPAM_PATTERNS = [
    re.compile(r"free\s+.*(mac\s*book|macbook|iphone|ps\s*5|playstation)\b", re.I),
    re.compile(r"tickets?\s+(?:to|for)\s+.+(concert|show|tour|event)", re.I),
    re.compile(r"\b(?:dm|pm|message|text)\s+me\b.*\b(interested|if interested)\b", re.I),
    re.compile(r"first\s*come\s*first\s*serve", re.I),
    re.compile(r"\bmail\s+me\b|\bemail\s+me\b", re.I),
]

SUSPICIOUS_TERMS = [
    ("dm_if_interested", 2, re.compile(r"\b(?:dm|message|text)\s+me\s+if\s+(?:(?:you'?re|you\s+are)\s+)?interested\b", re.I)),
    ("interested_should_dm", 2, re.compile(r"\b(?:any\s*one|anyone)\b.*\binterested\b.*\b(?:should|can|pls|please)?\s*(?:dm|pm|message|text)\b(?:\s+me)?\b", re.I)),
    ("gifting_device", 2, re.compile(r"\b(?:gift(?:ing)?(?:\s+out)?|giv(?:e|ing)\s+away|offer(?:ing)?(?:\s+it)?\s+(?:out|up)|pass(?:ing)?\s+(?:on|along))\b.*\b(?:mac\s*book|macbook|iphone|ps\s*5|playstation|laptop|camera|canon|nikon|eos|dslr|mirror\s*less|gopro|go\s*pro|drone|gimbal|lens(?:es)?|\d{2,3}\s*-\s*\d{2,3}\s*mm|\d{2,3}\s*mm|f/\d)\b", re.I)),
    ("sell_my", 1, re.compile(r"\bsell(?:ing)?\s+my\b", re.I)),
    ("season_tickets", 1, re.compile(r"\bseason\s+tickets?\b", re.I)),
    ("selling_tickets", 1, re.compile(r"\bsell(?:ing)?\s+(?:my\s+)?tickets?\b", re.I)),
    ("whatsapp", 1, re.compile(r"\bwhats?app\b", re.I)),
    #"Moving abroad, giving away my furniture for free, DM me" giveaway scam.
    ("relocating_pretext", 2, re.compile(r"\b(?:relocat\w*|moving|leaving)\b.{0,40}\b(?:new\s+(?:country|state|city|place)|abroad|overseas|out\s+of\s+(?:the\s+)?(?:country|state|town)|to\s+a\s+new)\b", re.I)),
    ("giving_away_items", 2, re.compile(r"\b(?:give\s+(?:out|away)|giving\s+(?:out|away|them\s+out)|giv(?:e|ing)\s+them\s+(?:out|away)|offer(?:ing)?(?:\s+it)?\s+(?:out|up)|pass(?:ing)?\s+(?:on|along))\b.{0,40}\b(?:item|items|belongings|stuff|everything|furniture|free)\b", re.I)),
    ("free_of_charge", 1, re.compile(r"\bfree\s+of\s+charge\b|\bcompletely\s+free\b|\bfor\s+free\b", re.I)),
    ("dm_me_for_details", 1, re.compile(r"\b(?:dm|pm|message|text|inbox)\s+me\s+for\s+(?:more\s+)?(?:detail|details|info|information|pic|pics|picture|pictures)\b", re.I)),
    ("send_me_a_message", 1, re.compile(r"\bsend\s+me\s+a\s+(?:message|dm|text|note)\b", re.I)),
]

#Household goods / appliances frequently dumped in the giveaway-scam item list.
HOUSEHOLD_ITEM_RE = re.compile(
    r"\b(?:trampoline|treadmill|freezer|fridge|refrigerator|washer|dryer|dishwasher|"
    r"microwave|mattress|dresser|couch|sofa|sectional|bunk\s*bed|kennel|"
    r"water\s+dispenser|kitchen\s*aid|stand\s*mixer|smart\s*tv|x\s*box|xbox|"
    r"ps\s*[45]|playstation|wardrobe|recliner|loveseat|ottoman|bookshelf|nightstand)\b",
    re.I,
)
#Distinct household items needed before the long-list density signal fires.
HOUSEHOLD_ITEM_THRESHOLD = 5

MIN_SPAM_SCORE = 3
#At/above this score the message is unambiguous spam and safe to auto-delete.
#Between MIN_SPAM_SCORE and here it's borderline: alert officers but leave the
#message up (unless other signals raise confidence) so a false positive never
#removes a real member's post.
HIGH_CONFIDENCE_SPAM_SCORE = 5

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"\+?1?\s*(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}")

try:
    from rapidfuzz import fuzz as rf_fuzz
    def _fuzzy_hit(text: str, phrase: str, thresh: int=88) -> bool:
        try:
            text_len = len(text.strip())
            phrase_len = len(phrase.strip())
            #Coverage check: message must be at least 50% of phrase length (IoU-like)
            if phrase_len > 0 and text_len / phrase_len < 0.5:
                return False
            return rf_fuzz.partial_ratio(text.lower(), phrase.lower()) >= thresh
        except Exception:  #rapidfuzz internal error; treat as no match
            return False
except Exception:  #rapidfuzz not installed; use simple substring fallback
    def _fuzzy_hit(text: str, phrase: str, thresh: int=88) -> bool:
        text_len = len((text or "").strip())
        phrase_len = len(phrase.strip())
        if phrase_len > 0 and text_len / phrase_len < 0.5:
            return False
        return phrase.lower() in (text or "").lower()

_MESSAGE_COUNTS: dict[Tuple[int, int], int] = defaultdict(int)
_MESSAGE_COUNTS_TS: dict[Tuple[int, int], float] = {}
_MESSAGE_COUNTS_EVICTION_INTERVAL_SEC = 3600
_MESSAGE_COUNTS_LAST_EVICTION: float = 0.0


def _evict_stale_message_counts() -> None:
    """Remove message count entries older than the eviction interval."""
    global _MESSAGE_COUNTS_LAST_EVICTION
    now = time.monotonic()
    if now - _MESSAGE_COUNTS_LAST_EVICTION < 600:
        return
    _MESSAGE_COUNTS_LAST_EVICTION = now
    cutoff = now - _MESSAGE_COUNTS_EVICTION_INTERVAL_SEC
    stale = [k for k, ts in _MESSAGE_COUNTS_TS.items() if ts < cutoff]
    for k in stale:
        _MESSAGE_COUNTS.pop(k, None)
        _MESSAGE_COUNTS_TS.pop(k, None)


def _message_count_key(message) -> Tuple[int, int]:
    """Produce a (guild_id, user_id) tuple for per-user spam heuristics."""
    try:
        guild_id = int(getattr(getattr(message, 'guild', None), 'id', 0) or 0)
    except Exception:  #malformed guild attr; default to 0
        guild_id = 0
    try:
        user_id = int(getattr(getattr(message, 'author', None), 'id', 0) or 0)
    except Exception:  #malformed author attr; default to 0
        user_id = 0
    return (guild_id, user_id)

def _has_privileged_role(member, settings) -> Optional[str]:
    """Check for role-based exemptions and explain the trust reason."""
    try:
        roles = getattr(member, 'roles', []) or []
        trusted_list = [s.lower() for s in (getattr(settings, 'trusted_role_names', []) or [])]
        for r in roles:
            name = str(getattr(r, 'name', '')).lower()
            if any(token in name for token in trusted_list):
                return "trusted_role"
    except Exception:  #role access failed; assume no bypass
        pass
    return None


def _is_trusted_member(message, settings, *, prior_count: int = 0) -> Optional[str]:
    """Apply role/account-age/message-count gates before running spam rules."""
    try:
        member = getattr(message, 'author', None)
        if not member:
            return None
        if is_officer(member, settings):
            return "trusted_officer"
        msg_threshold = int(getattr(settings, 'spam_trust_message_threshold', 50) or 50)
        if prior_count >= msg_threshold:
            return "trusted_message_count"
        #Trusted roles
        role_reason = _has_privileged_role(member, settings)
        if role_reason:
            return role_reason
        return None
    except Exception:  #trust evaluation failed; do not trust
        return None

def check_spam(message, settings) -> tuple[bool, str, int]:
    """Main spam check: returns (is_spam, reason, score).

    ``score`` is the accumulated suspicion weight; callers use it to gauge
    confidence (e.g. delete only above a higher bar, alert-only below it).
    """
    _evict_stale_message_counts()
    text = (getattr(message, 'content', None) or '').strip()
    if not text:
        return (False, "empty", 0)
    key = _message_count_key(message)
    prior_count = _MESSAGE_COUNTS.get(key, 0)
    _MESSAGE_COUNTS[key] = prior_count + 1
    _MESSAGE_COUNTS_TS[key] = time.monotonic()
    trust = _is_trusted_member(message, settings, prior_count=prior_count)
    if trust:
        return (False, trust, 0)
    #Strong indicators
    contact_hits = []
    score = 0
    if EMAIL_RE.search(text):
        score += 2
        contact_hits.append("email")
    if PHONE_RE.search(text):
        score += 2
        contact_hits.append("phone")

    suspicion_hits = []
    for name, weight, rx in SUSPICIOUS_TERMS:
        if rx.search(text):
            score += weight
            suspicion_hits.append(name)

    matched_rules = []
    for rx in SPAM_PATTERNS:
        if rx.search(text):
            score += 2
            matched_rules.append(rx.pattern)
    #Long list of household goods is the tell for the "moving abroad" giveaway scam.
    item_hits = {m.group(0).lower() for m in HOUSEHOLD_ITEM_RE.finditer(text)}
    item_count = len(item_hits)
    if item_count >= HOUSEHOLD_ITEM_THRESHOLD:
        score += 2
    #fuzzy phrases
    fuzzy_phrases = [
        "tickets available", "4 tickets", "american airlines center",
        "dm me if interested", "message me if interested", "first come first serve",
        "free macbook", "giving out my macbook", "free iphone","at&t stadium", "ps5 charger",
        "relocating to a new country", "give out my items", "free of charge",
        "moving abroad", "everything must go", "excellent condition",
    ]
    fuzzy_hits = []
    for ph in fuzzy_phrases:
        if _fuzzy_hit(text, ph, 86):
            score += 1
            fuzzy_hits.append(ph)
    #Logging for visibility
    try:
        author_id = getattr(getattr(message, 'author', None), 'id', 'unknown')
        channel_id = getattr(getattr(message, 'channel', None), 'id', 'unknown')
        details_parts = []
        if contact_hits:
            details_parts.append(f"contact={','.join(contact_hits)}")
        if suspicion_hits:
            details_parts.append(f"phrases={','.join(suspicion_hits)}")
        if matched_rules:
            details_parts.append(f"regex={'|'.join(matched_rules)}")
        if item_count >= HOUSEHOLD_ITEM_THRESHOLD:
            details_parts.append(f"items={item_count}")
        if fuzzy_hits:
            details_parts.append(f"fuzzy={len(fuzzy_hits)}")
        if details_parts:
            detail_msg = "; ".join(details_parts)
            if score >= MIN_SPAM_SCORE:
                log_action("spam_detect_debug", f"user={author_id}; ch={channel_id}", f"score={score}; {detail_msg}")
            else:
                log_action("spam_watch", f"user={author_id}; ch={channel_id}", f"score={score}; {detail_msg}")
    except Exception:  #logging failure must not block spam detection
        pass

    if score >= MIN_SPAM_SCORE:
        return (True, "rules", score)
    return (False, "none", score)

