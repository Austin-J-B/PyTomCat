"""Typed helpers for CatDatabase + RecentPics tabs.

Expected headers (by column index) based on the current sheet:
- CatDatabase: ["67. Microwave", ID_HELPER, LAST_SEEN_DATE, LAST_SEEN_TIME, LAST_SEEN_BY, (spacer), MOST_RECENT_IMAGE_URL,
  LOCATION, PHYSICAL_DESCRIPTION, BIRTHDAY_ESTIMATE, BEHAVIOR, TNRD?, TNR_DATE, SEX, COMMON_NICKNAMES, COMMENTS]
- RecentPics: [FULL_NAME (e.g., "67. Microwave"), <unused>, TOTAL, URL1, SERIAL1, URL2, SERIAL2, ...]
"""
from __future__ import annotations
from typing import Any
import datetime as dt
import json, time
from .sheets_client import sheets_client
from ..config import settings
try:
    from ..utils.text import norm_alnum_lower  #real helper if you have utils/
except Exception:
    import re as _re
    def norm_alnum_lower(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", (s or "").lower())
import re #Ensure this is imported at the top of the file
from pathlib import Path

#Configuration for "TCB Pics Formatted" columns (0-based index)
COL_LABEL = 0   #Officer ID / Name
COL_URL = 6     #Picture Link
COL_SERIAL = 7  #Serial number

_TCB_ROWS: list[list[str]] | None = None
_TCB_TS: float = 0.0  #monotonic for in-memory
_TCB_SNAPSHOT = Path("cache") / "sheets" / "tcb_pics_formatted.json"


def _normalize_rows(rows: Any) -> list[list[str]]:
    """Normalize raw sheet data into a stable list[list[str]] shape."""
    if not isinstance(rows, list):
        return []
    out: list[list[str]] = []
    for row in rows:
        if isinstance(row, list):
            out.append(["" if v is None else str(v) for v in row])
        elif isinstance(row, tuple):
            out.append(["" if v is None else str(v) for v in list(row)])
        elif row is None:
            out.append([])
        else:
            out.append([str(row)])
    return out


def _fetch_tcb_rows_live() -> list[list[str]]:
    """Fetch TCB rows from Sheets with fallback to raw values_get if gspread helpers fail."""
    sid = settings.sheet_catabase_id
    if not sid:
        return []

    gc = sheets_client()
    sh = gc.open_by_key(sid)
    ws = sh.worksheet("TCB Pics Formatted")

    # Primary path: gspread convenience API.
    try:
        return _normalize_rows(ws.get_all_values())
    except Exception:
        pass

    # Fallback path: raw Sheets values_get avoids gspread fill_gaps edge cases.
    try:
        raw = sh.values_get("'TCB Pics Formatted'")
        return _normalize_rows((raw or {}).get("values") or [])
    except Exception:
        pass

    # Last fallback with explicit range in case tab-level fetch is blocked.
    try:
        raw = sh.values_get("'TCB Pics Formatted'!A:ZZ")
        return _normalize_rows((raw or {}).get("values") or [])
    except Exception:
        return []

def _load_tcb_snapshot() -> tuple[list[list[str]] | None, float]:
    """Load snapshot from disk. Returns (rows, unix_timestamp)."""
    try:
        data = json.loads(_TCB_SNAPSHOT.read_text(encoding="utf-8"))
        rows = data.get("rows")
        ts = float(data.get("ts") or 0.0)
        if isinstance(rows, list):
            return rows, ts
    except Exception:
        pass
    return None, 0.0

def _write_tcb_snapshot(rows: list[list[str]]) -> None:
    """Write snapshot to disk with current Unix timestamp."""
    try:
        _TCB_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _TCB_SNAPSHOT.write_text(json.dumps({"ts": time.time(), "rows": rows}), encoding="utf-8")
    except Exception:
        pass

def force_refresh_tcb_cache() -> list[list[str]]:
    """Force refresh the TCB pics cache from the live sheet, bypassing TTL."""
    global _TCB_ROWS, _TCB_TS
    try:
        rows = _fetch_tcb_rows_live()
        if not rows:
            return _TCB_ROWS or []
        _TCB_ROWS, _TCB_TS = rows, time.monotonic()
        _write_tcb_snapshot(rows)
        return rows
    except Exception:
        return _TCB_ROWS or []

def get_tcb_pics_rows(ttl_sec: int | None = None) -> list[list[str]]:
    """Fetch TCB Pics Formatted rows with in-memory + on-disk caching to reduce API reads."""
    global _TCB_ROWS, _TCB_TS
    ttl = int(ttl_sec if ttl_sec is not None else getattr(settings, "show_sheet_recentpics_ttl_sec", 300) or 300)
    ttl = max(1, ttl)
    now_mono = time.monotonic()
    now_unix = time.time()
    #In-memory cache (uses monotonic, session-only)
    if _TCB_ROWS is not None and (now_mono - _TCB_TS) < ttl:
        return _TCB_ROWS
    #On-disk snapshot (uses Unix epoch, survives restarts)
    snap_rows, snap_ts = _load_tcb_snapshot()
    if snap_rows is not None and (now_unix - snap_ts) < ttl:
        _TCB_ROWS, _TCB_TS = snap_rows, now_mono
        return snap_rows
    #Fetch live sheet
    try:
        rows = _fetch_tcb_rows_live()
        if not rows:
            return snap_rows or _TCB_ROWS or []
        _TCB_ROWS, _TCB_TS = rows, now_mono
        _write_tcb_snapshot(rows)
        return rows
    except Exception:
        #Fallback to whatever snapshot we have
        if snap_rows is not None:
            _TCB_ROWS, _TCB_TS = snap_rows, now_mono
            return snap_rows
        return []

async def _get_all_photos_long_format():
    """Helper to fetch the master photo list from Catabase."""
    rows = get_tcb_pics_rows()
    return rows[1:] if rows else []  #Skip header

async def get_most_recent_photo(full_name: str, _retried: bool = False) -> dict | str:
    """Fetch the most recent photo row for a cat from the long-format list.
    
    If no photos found, will force-refresh cache and retry once (smart recache).
    """
    try:
        rows = await _get_all_photos_long_format()
    except Exception as e:
        return f"Sheet error: {e}"

    key = norm_alnum_lower(full_name)
    matches = []
    
    for r in rows:
        #Safety check for row length
        if len(r) <= COL_SERIAL: continue
        
        if norm_alnum_lower(r[COL_LABEL]) == key:
            if r[COL_URL].startswith("http"):
                matches.append(r)

    if not matches:
        #Smart recache: force refresh and retry once
        if not _retried:
            force_refresh_tcb_cache()
            return await get_most_recent_photo(full_name, _retried=True)
        return f"No photos found for {full_name}."

    #Sort by Serial Number (descending)
    def parse_serial(row):
        try:
            #Remove non-digits (e.g. "sn123" -> 123)
            val = row[COL_SERIAL]
            return int(re.sub(r"\D", "", val) or 0)
        except:
            return 0

    best_row = max(matches, key=parse_serial)
    
    return {
        "actual_name": best_row[COL_LABEL],
        "url": best_row[COL_URL],
        "serial": best_row[COL_SERIAL],
        "total_available": len(matches)
    }

#Optional: Update get_recent_photo to use the new source too
async def get_recent_photo(full_name: str, _retried: bool = False) -> dict | str:
    """Pick a random recent photo from the full history.
    
    If no photos found, will force-refresh cache and retry once (smart recache).
    """
    try:
        rows = await _get_all_photos_long_format()
    except Exception as e:
        return f"Sheet error: {e}"

    key = norm_alnum_lower(full_name)
    matches = [r for r in rows if len(r) > COL_SERIAL and norm_alnum_lower(r[COL_LABEL]) == key and r[COL_URL].startswith("http")]

    if not matches:
        #Smart recache: force refresh and retry once
        if not _retried:
            force_refresh_tcb_cache()
            return await get_recent_photo(full_name, _retried=True)
        return f"No recent photos for '{full_name}'."

    import random
    
    #Sort by serial ascending (oldest=lowest serial first)
    def parse_serial(row):
        try:
            return int(re.sub(r"\D", "", row[COL_SERIAL]) or 0)
        except:
            return 0
    
    matches.sort(key=parse_serial)
    pick = random.choice(matches)
    pick_idx = matches.index(pick)  #0-based position in sorted list
    
    return {
        "actual_name": pick[COL_LABEL],
        "url": pick[COL_URL],
        "serial": pick[COL_SERIAL],
        "total_available": len(matches),
        "reverse_index": pick_idx + 1  #1-based: oldest=1, newest=total
    }




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
    """Return profile metadata for a cat or a friendly error string."""
    """Return a dict for a cat profile or a string error message."""
    if not settings.sheet_catabase_id:
        return "Catabase sheet ID not configured. Set SHEET_CATABASE_ID in .env."
    gc = sheets_client()
    ws = gc.open_by_key(settings.sheet_catabase_id).worksheet("CatDatabase")

    rows = ws.get_all_values()
    if not rows:
        return "Catabase is empty."

    #Build lookup by normalized key: "67. Microwave" → "67microwave" etc
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
        #Fallback: try without leading digits and punctuation
        name_only = "".join(ch for ch in full_name if not ch.isdigit()).lstrip(". ").strip()
        if norm_alnum_lower(name_only) == key:
            best_row = r
            break

    if not best_row:
        return f"No match for '{query}'."

    #Compute approximate age from birthday_estimate if formatted like M/D/YYYY
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



async def get_random_photo(full_name: str):
    """Pick a random photo record for variety in responses."""
    return await get_recent_photo(full_name)

async def build_profile_embed(query: str) -> dict | str:
    """Construct the embeds used for the "who is" command."""
    """
    Returns a dict compatible with discord.Embed.from_dict or a string error.
    Uses CatDatabase for metadata and RecentPics for a nice image if available.
    """
    prof = await get_cat_profile(query)
    if isinstance(prof, str):
        return prof  #error string from get_cat_profile

    #Prefer most-recent photo; fall back to CatDatabase image_url
    recent = await get_most_recent_photo(prof["actual_name"])
    img_url = None
    if isinstance(recent, dict) and recent.get("url"):
        img_url = recent["url"]
    elif prof.get("image_url"):
        img_url = prof["image_url"]

    #Match the legacy dense profile card style used in cats-on-campus.
    display = re.sub(r"^\s*\d+\.\s*", "", str(prof.get("actual_name") or query)).strip()
    lines: list[str] = []

    desc = prof.get("physical_description")
    if desc:
        lines.append(f"**Description:** {desc}")
    behavior = prof.get("behavior")
    if behavior:
        lines.append(f"**Behavior:** {behavior}")
    location = prof.get("location")
    if location:
        lines.append(f"**Location:** {location}")
    age = prof.get("age")
    if age:
        lines.append(f"**Age Estimate:** {age}")
    sex = prof.get("sex")
    if sex:
        lines.append(f"**Sex:** {sex}")
    tnr = prof.get("tnrd")
    if tnr:
        lines.append(f"**TNR Status:** {tnr}")
    tnr_date = prof.get("tnr_date")
    if tnr_date:
        lines.append(f"**TNR Date:** {tnr_date}")

    last_bits = []
    if prof.get("last_seen_date"):
        last_bits.append(str(prof["last_seen_date"]))
    if prof.get("last_seen_time"):
        last_bits.append(str(prof["last_seen_time"]))
    if prof.get("last_seen_by"):
        last_bits.append(f"by {prof['last_seen_by']}")
    if last_bits:
        lines.append("**Last Reported:** " + " ".join(last_bits))

    nicknames = prof.get("nicknames")
    if nicknames:
        lines.append(f"**Common Nicknames:** {nicknames}")
    comments = prof.get("comments")
    if comments:
        lines.append(f"**Comments:** {comments}")

    embed = {
        "title": f"__**{display}**__",
        "color": 0x2F3136,
        "description": "\n".join(lines),
    }
    if img_url:
        embed["image"] = {"url": img_url}
    return embed
