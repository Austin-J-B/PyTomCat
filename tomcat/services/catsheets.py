"""Typed helpers for CatDatabase + RecentPics tabs.

Expected headers (by column index) inspired by your current sheet:
- CatDatabase: ["67. Microwave", ID_HELPER, LAST_SEEN_DATE, LAST_SEEN_TIME, LAST_SEEN_BY, (spacer), MOST_RECENT_IMAGE_URL,
  LOCATION, PHYSICAL_DESCRIPTION, BIRTHDAY_ESTIMATE, BEHAVIOR, TNRD?, TNR_DATE, SEX, COMMON_NICKNAMES, COMMENTS]
- RecentPics: [FULL_NAME (e.g., "67. Microwave"), <unused>, TOTAL, URL1, SERIAL1, URL2, SERIAL2, ...]
"""
from __future__ import annotations
from typing import Any
import datetime as dt
from .sheets_client import sheets_client
from ..config import settings
try:
    from ..utils.text import norm_alnum_lower  # real helper if you have utils/
except Exception:
    import re as _re
    def norm_alnum_lower(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", (s or "").lower())

IDX = {
    "full_name": 0,
    "id_helper": 1,
    "last_seen_date": 2,
    "last_seen_time": 3,
    "last_seen_by": 4,
    "spacer": 5,
    "image_url": 6,
    "location": 7,
    "physical_description": 8,
    "birthday_estimate": 9,
    "behavior": 10,
    "tnrd": 11,
    "tnr_date": 12,
    "sex": 13,
    "nicknames": 14,
    "comments": 15,
}

async def get_cat_profile(query: str) -> dict | str:
    """Return a dict for a cat profile or a string error message."""
    if not settings.sheet_catabase_id:
        return "Catabase sheet ID not configured. Set SHEET_CATABASE_ID in .env."
    gc = sheets_client()
    ws = gc.open_by_key(settings.sheet_catabase_id).worksheet("CatDatabase")

    rows = ws.get_all_values()
    if not rows:
        return "Catabase is empty."

    # Build lookup by normalized key: "67. Microwave" → "67microwave" etc
    header, *data = rows
    best_row = None
    key = norm_alnum_lower(query)
    if not key:
        return "Empty query."

    for r in data:
        full_name = (r[IDX["full_name"]] if len(r) > IDX["full_name"] else "") or ""
        if norm_alnum_lower(full_name) == key:
            best_row = r
            break
        # Fallback: try without leading digits and punctuation
        name_only = "".join(ch for ch in full_name if not ch.isdigit()).lstrip(". ").strip()
        if norm_alnum_lower(name_only) == key:
            best_row = r
            break

    if not best_row:
        return f"No match for '{query}'."

    # Compute approximate age from birthday_estimate if formatted like M/D/YYYY
    age = None
    try:
        b = best_row[IDX["birthday_estimate"]] if len(best_row) > IDX["birthday_estimate"] else ""
        if b:
            m, d, y = [int(x) for x in str(b).split("/")]
            bd = dt.date(y, m, d)
            today = dt.date.today()
            years = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            age = f"~{years} years old"
    except Exception:
        pass

    return {
        "actual_name": best_row[IDX["full_name"]].strip() if len(best_row) > IDX["full_name"] else query.strip(),
        "image_url": best_row[IDX["image_url"]] if len(best_row) > IDX["image_url"] else None,
        "physical_description": best_row[IDX["physical_description"]] if len(best_row) > IDX["physical_description"] else None,
        "behavior": best_row[IDX["behavior"]] if len(best_row) > IDX["behavior"] else None,
        "location": best_row[IDX["location"]] if len(best_row) > IDX["location"] else None,
        "last_seen_date": best_row[IDX["last_seen_date"]] if len(best_row) > IDX["last_seen_date"] else None,
        "last_seen_time": best_row[IDX["last_seen_time"]] if len(best_row) > IDX["last_seen_time"] else None,
        "last_seen_by": best_row[IDX["last_seen_by"]] if len(best_row) > IDX["last_seen_by"] else None,
        "age": age,
        "tnrd": best_row[IDX["tnrd"]] if len(best_row) > IDX["tnrd"] else None,
        "tnr_date": best_row[IDX["tnr_date"]] if len(best_row) > IDX["tnr_date"] else None,
        "sex": best_row[IDX["sex"]] if len(best_row) > IDX["sex"] else None,
        "nicknames": best_row[IDX["nicknames"]] if len(best_row) > IDX["nicknames"] else None,
        "comments": best_row[IDX["comments"]] if len(best_row) > IDX["comments"] else None,
    }

async def get_recent_photo(full_name: str) -> dict | str:
    """Pick one recent photo for a given FULL_NAME from RecentPics tab."""
    if not settings.sheet_vision_id:
        return "Aux sheet ID not configured. Set SHEET_VISION_ID in .env."
    gc = sheets_client()
    ws = gc.open_by_key(settings.sheet_vision_id).worksheet("RecentPics")

    rows = ws.get_all_values()
    key = norm_alnum_lower(full_name)
    if not rows or not key:
        return "No data."

    header, *data = rows
    matches = [r for r in data if norm_alnum_lower(r[0] if r else "") == key]
    if not matches:
        return f"No recent photos for '{full_name}'."

    pick = max(matches, key=lambda r: int(r[2] or 0) if len(r) > 2 and str(r[2]).isdigit() else 0)
    total_available = int(pick[2] or 0) if len(pick) > 2 and str(pick[2]).isdigit() else 0

    # Collect URL/SERIAL pairs starting at col 3
    pairs: list[tuple[str, str]] = []
    i = 3
    while i < len(pick):
        url = pick[i].strip() if i < len(pick) else ""
        serial = pick[i + 1].strip() if i + 1 < len(pick) else ""
        if url:
            pairs.append((url, serial or "Unknown"))
        i += 2
    if not pairs:
        return f"No accessible photos found for {full_name}."

    import random
    url, serial = random.choice(pairs)
    reverse_index = max(total_available - pairs.index((url, serial)), 1)
    return {
        "actual_name": full_name,
        "url": url,
        "serial": serial,
        "total_available": total_available,
        "reverse_index": reverse_index,
    }


async def get_random_photo(full_name: str):
    return await get_recent_photo(full_name)