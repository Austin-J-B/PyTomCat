"""Datetime helpers shared across dues + finance writers."""

from __future__ import annotations

import os
from datetime import datetime


def format_mmddyyyy(dt: datetime) -> str:
    """Return US-style M/D/YYYY with platform-compatible specifiers."""
    if os.name == "nt":
        return dt.strftime("%#m/%#d/%Y")
    return dt.strftime("%-m/%-d/%Y")
