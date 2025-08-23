"""Channel-first + regex classifier with wake-word support.
Add DeBERTa fallback later only when classify() returns None.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional
from .config import settings

WAKE = re.compile(rf"^\s*{re.escape(settings.tomcat_wake)}[\s,]+", re.I)
PAT_CAT_SHOW = re.compile(r"show\s+(?:me\s+)?(?P<name>[\w \-']+)$", re.I)
PAT_FEEDING_STATUS = re.compile(r"(feeding|stations?)\s+(status|today|list)", re.I)
PAT_SUB_REQUEST = re.compile(r"\b(sub|substitute)\b.*\b(for|at)\b.*", re.I)

@dataclass
class Intent:
    type: str
    data: dict

def _strip_wake(text: str) -> str:
    m = WAKE.match(text or "")
    return text[m.end():].strip() if m else text

def classify(text: str, channel_id: Optional[int] = None) -> Optional[Intent]:
    """Return an Intent or None."""
    if not text:
        return None
    body = _strip_wake(text)

    # 1) Direct commands
    m = PAT_CAT_SHOW.search(body)
    if m:
        return Intent("cat_show", {"name": m.group("name").strip()})
    if PAT_FEEDING_STATUS.search(body):
        return Intent("feeding_status", {})
    if PAT_SILENT.search(body):
        return Intent("silent_mode", {})

    # 2) Prefix commands like !members (extend as needed)
    if text.startswith(settings.command_prefix):
        cmd = text[len(settings.command_prefix):].strip().split()[0].lower()
        if cmd == "members":
            return Intent("members_count", {})

    # 3) No confident rule match
    return None