"""Channel-first + regex classifier with wake-word support.
Add DeBERTa fallback later only when classify() returns None."""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional
from .config import settings

WAKE = re.compile(rf"^\s*{re.escape(settings.bot_name)}[\s,]+", re.I)
PAT_CAT_SHOW = re.compile(r"show\s+(?:me\s+)?(?P<name>[\w \-']+)$", re.I)
PAT_FEEDING_STATUS = re.compile(r"(feeding|stations?)\s+(status|today|list)", re.I)
PAT_SUB_REQUEST = re.compile(r"\b(sub|substitute)\b.*\b(for|at)\b.*", re.I)
PAT_SILENT = re.compile(r"silent\s+mode", re.I)

@dataclass
class Intent:
    type: str
    args: dict

def classify(channel_id: int, content: str) -> Optional[Intent]:
    text = (content or "").strip()

    # 1) Channel-first rules
    if settings.ch_feeding_team and channel_id == settings.ch_feeding_team:
        if PAT_FEEDING_STATUS.search(text):
            return Intent("feeding_status", {})
        if PAT_SUB_REQUEST.search(text):
            return Intent("feeding_sub_request", {})

    if settings.ch_dues_portal and channel_id == settings.ch_dues_portal:
        if re.search(r"(@\w+|\$\w+|paypal|venmo|cash\s*app|receipt)", text, re.I):
            return Intent("dues_notice", {"raw": text})

    # 2) Wake-word path: "TomCat, ..."
    if WAKE.match(text):
        body = WAKE.sub("", text).strip()
        m = PAT_CAT_SHOW.search(body)
        if m:
            return Intent("cat_show", {"name": m.group("name").strip()})
        if PAT_FEEDING_STATUS.search(body):
            return Intent("feeding_status", {})
        if PAT_SILENT.search(body):
            return Intent("silent_mode", {})

    # 3) Prefix commands like !members (add as needed)
    if text.startswith(settings.command_prefix):
        cmd = text[len(settings.command_prefix):].strip().split()[0].lower()
        if cmd == "members":
            return Intent("members_count", {})

    # 4) No confident rule match → None (later: try NLU fallback)
    return None
