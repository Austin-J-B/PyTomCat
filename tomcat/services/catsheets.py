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
from . .config import settings
from . .utils.text import norm_alnum_lower

IDX = {
    "full_name": 0,
    "image_url": 6,
    "location": 7,
    "physical_description": 8,
    "birthday": 9,
    "behavior": 10,
    "tnr_status": 11,
    "tnr_date": 12,
    "sex": 13,
    "nicknames": 14,
    "last_seen_date": 2,
    "last_seen_time": 3,
    "last_seen_by": 4,
}

_cache: dict[str, Any] = {"cat_rows": None, "cat_stamp": 0, "pics_rows": None, "pics_stamp": 0}
_TTL = 5 * 60  # seconds

async def get_cat_profile(query: str) -> dict | str:
    if not settings.cat_spreadsheet_id:
        return "Catabase sheet ID not configured. Set CAT_SPREADSHEET_ID in .env."
    gc = sheets_client()
    ws = gc.open_by_key(settings.cat_spreadsheet_id).worksheet("CatDatabase")

    now = dt.datetime.now().timestamp()
    if not _cache["cat_rows"] or now - _cache["cat_stamp"] > _TTL:
        _cache["cat_rows"] = ws.get_all_values()
        _cache["cat_stamp"] = now
    rows = _cache["cat_rows"] or []

    q = query.strip()
    q_norm = norm_alnum_lower(q)
    pick = None
    for r in rows:
        if not r: continue
        full = (r[IDX["full_name"]] if len(r) > IDX["full_name"] else "").strip()
        if not full: continue
        if q.lower() in full.lower() or q_norm in norm_alnum_lower(full):
            pick = r
            break
    if not pick:
        return f"I couldn't find any information about a \"{query}\"."

    full = pick[IDX["full_name"]]
    actual_name = " ".join(full.split(".")[1:]).strip() if "." in full else full

    # Age estimate
    age = "Unknown"
    try:
        b = pick[IDX["birthday"]]
        if b:
            m, d, y = [int(x) for x in str(b).split("/")]
            bd = dt.date(y, m, d)
            today = dt.date.today()
            years = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            age = f"~{years} years old"
    except Exception:
        pass

    return {
        "actual_name": actual_name,
        "image_url": pick[IDX["image_url"]] if len(pick) > IDX["image_url"] else None,
        "physical_description": pick[IDX["physical_description"]] if len(pick) > IDX["physical_description"] else None,
        "behavior": pick[IDX["behavior"]] if len(pick) > IDX["behavior"] else None,
        "location": pick[IDX["location"]] if len(pick) > IDX["location"] else None,
        "age_estimate": age,
        "tnr_status": (pick[IDX["tnr_status"]] or "Unknown") if len(pick) > IDX["tnr_status"] else "Unknown",
        "nicknames": pick[IDX["nicknames"]] if len(pick) > IDX["nicknames"] else None,
        "last_seen_date": pick[IDX["last_seen_date"]] if len(pick) > IDX["last_seen_date"] else None,
        "last_seen_time": pick[IDX["last_seen_time"]] if len(pick) > IDX["last_seen_time"] else None,
        "last_seen_by": pick[IDX["last_seen_by"]] if len(pick) > IDX["last_seen_by"] else None,
    }

async def get_random_photo(query: str) -> dict | str:
    if not settings.aux_spreadsheet_id:
        return "Aux sheet ID not configured. Set AUX_SPREADSHEET_ID in .env."
    gc = sheets_client()
    ws = gc.open_by_key(settings.aux_spreadsheet_id).worksheet("RecentPics")

    now = dt.datetime.now().timestamp()
    if not _cache["pics_rows"] or now - _cache["pics_stamp"] > _TTL:
        _cache["pics_rows"] = ws.get_all_values()
        _cache["pics_stamp"] = now
    rows = _cache["pics_rows"] or []

    q_norm = norm_alnum_lower(query)
    row = None
    for r in rows:
        if not r: continue
        full = (r[0] if len(r) > 0 else "").strip()
        if q_norm in norm_alnum_lower(full):
            row = r
            break
    if not row:
        return f"I couldn't find a cat named \"{query}\"."

    full = row[0]
    actual_name = " ".join(full.split(".")[1:]).strip() if "." in full else full
    total_available = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
    if total_available == 0:
        return f"I couldn't find any photos of {actual_name}."

    # From column D onward: url, serial, url, serial, ...
    pairs: list[tuple[str, str]] = []
    i = 3
    while i + 1 < len(row):
        url = row[i]
        serial = row[i + 1]
        if url:
            pairs.append((url, serial or "Unknown"))
        i += 2
    if not pairs:
        return f"No accessible photos found for {actual_name}."

    import random
    url, serial = random.choice(pairs)
    reverse_index = max(total_available - pairs.index((url, serial)), 1)
    return {
        "actual_name": actual_name,
        "url": url,
        "serial": serial,
        "total_available": total_available,
        "reverse_index": reverse_index,
    }