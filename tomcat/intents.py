"""Channel-first + regex classifier with wake-word support.
Add DeBERTa fallback later only when classify() returns None.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional
from .config import settings

WAKE = re.compile(rf"^\s*{re.escape(settings.tomcat_wake)}[\s,]+", re.I)
PAT_WHO_IS = re.compile(r"who\s+is\s+(?P<name>[\w \-']+)$", re.I)
PAT_SHOW_PHOTO = re.compile(r"show\s+(?:me\s+)?(?P<name>[\w \-']+)$", re.I)
PAT_FEEDING_STATUS = re.compile(r"(feeding|stations?)\s+(status|today|list)", re.I)
PAT_SUB_REQUEST = re.compile(r"\b(sub|substitute)\b.*\b(for|at)\b.*", re.I)
PAT_SILENT = re.compile(r"\b(silent|quiet|no\s*reply)\b", re.I)
PAT_CV_DETECT = re.compile(r"\b(detect|detect\s+cats?)\b", re.I)
PAT_CV_CROP = re.compile(r"\b(crop|crop\s+cats?)\b", re.I)
PAT_CV_IDENT = re.compile(r"\b(identify|who\s+is\s+this)\b", re.I)

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
    m = PAT_WHO_IS.search(body)
    if m:
        return Intent("cat_profile", {"name": m.group("name").strip()})
    m = PAT_SHOW_PHOTO.search(body)
    if m:
        return Intent("cat_photo", {"name": m.group("name").strip()})
    
    if PAT_FEEDING_STATUS.search(body):
        return Intent("feeding_status", {})
    if PAT_SILENT.search(body):
        return Intent("silent_mode", {})
    if PAT_CV_IDENT.search(body):
        return Intent("cv_identify", {})
    if PAT_CV_CROP.search(body):
        return Intent("cv_crop", {})
    if PAT_CV_DETECT.search(body):
        return Intent("cv_detect", {})

    # 2) Prefix commands like !members (extend as needed)
    if text.startswith(settings.command_prefix):
        cmd = text[len(settings.command_prefix):].strip().split()[0].lower()
        if cmd == "members":
            return Intent("members_count", {})

    # 3) No confident rule match
    return None