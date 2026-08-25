"""Typed helpers for CatDatabase + local photo metadata data sources.

Expected headers (by column index) based on the current data sources:
- CatDatabase: ["67. Microwave", ID_HELPER, LAST_SEEN_DATE, LAST_SEEN_TIME, LAST_SEEN_BY, (spacer), MOST_RECENT_IMAGE_URL,
  LOCATION, PHYSICAL_DESCRIPTION, BIRTHDAY_ESTIMATE, BEHAVIOR, TNRD?, TNR_DATE, SEX, COMMON_NICKNAMES, COMMENTS]
- Local photo metadata snapshot: [CAT_LABEL, <unused>, <unused>, <unused>, <unused>, <unused>, DISCORD_URL,
  SERIAL_NUMBER, BOX_COORDINATES, BOX_CAT_IDS, LABEL_AUTHOR, COMMENTS]
"""
from __future__ import annotations
from typing import Any
import csv
import datetime as dt
import json, time
from .sheets_client import sheets_client
from . import local_photos
from ..config import settings
try:
    from ..utils.text import norm_alnum_lower
except Exception:
    import re as _re
    def norm_alnum_lower(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", (s or "").lower())
import re
from pathlib import Path

#Photo metadata row columns (0-based index)
COL_LABEL = 0   #Officer ID / Name
COL_URL = 6     #Picture Link
COL_SERIAL = 7  #Serial number
COL_BOX_CAT_IDS = 9

_CAT_PREFIX_RE = re.compile(r"^\s*\d+\s*[.)\-:]?\s*")
_LABEL_SPLIT_RE = re.compile(r"[|,;/]+")

_PHOTO_METADATA_ROWS_CACHE: list[list[str]] | None = None
_PHOTO_METADATA_ROWS_TS: float = 0.0  # monotonic for in-memory cache
#(mtime_ns, size) of the metadata CSV the cached rows were built from, or None
#when the rows came from the on-disk snapshot and the source state is unknown.
_PHOTO_METADATA_ROWS_SIG: tuple[int, int] | None = None
_PHOTO_METADATA_SNAPSHOT = Path("cache") / "sheets" / "photo_metadata_rows.json"
_PHOTO_METADATA_INDEX_CACHE: dict[str, list[list[str]]] | None = None
_PHOTO_METADATA_INDEX_TS: float = 0.0


def _normalize_rows(rows: Any) -> list[list[str]]:
    """Normalize raw row data into a stable list[list[str]] shape."""
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


def _display_label(value: str) -> str:
    """Drop CatDatabase numeric prefixes while preserving the CV label text."""
    text = str(value or "").strip()
    return _CAT_PREFIX_RE.sub("", text, count=1).strip()


def _photo_query_key(value: str) -> str:
    """Normalize cat names so CatDatabase IDs and CV labels compare cleanly."""
    return norm_alnum_lower(_display_label(value))


def _photo_row_label_tokens(row: list[str]) -> list[str]:
    """Extract all candidate cat-label tokens from one local photo metadata row."""
    raw_labels = ""
    if len(row) > COL_BOX_CAT_IDS:
        raw_labels = str(row[COL_BOX_CAT_IDS] or "").strip()
    if not raw_labels and len(row) > COL_LABEL:
        raw_labels = str(row[COL_LABEL] or "").strip()
    if not raw_labels:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _LABEL_SPLIT_RE.split(raw_labels):
        token = _display_label(raw)
        key = norm_alnum_lower(token)
        if not token or not key or key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _photo_row_match_details(row: list[str], query_key: str) -> list[dict[str, Any]]:
    """Return ordered match metadata for one row's box labels."""
    raw_labels = ""
    if len(row) > COL_BOX_CAT_IDS:
        raw_labels = str(row[COL_BOX_CAT_IDS] or "").strip()
    if not raw_labels and len(row) > COL_LABEL:
        raw_labels = str(row[COL_LABEL] or "").strip()
    if not raw_labels or not query_key:
        return []
    matches: list[dict[str, Any]] = []
    for idx, raw in enumerate(_LABEL_SPLIT_RE.split(raw_labels), start=1):
        token = _display_label(raw)
        key = norm_alnum_lower(token)
        if not token or not key or key != query_key:
            continue
        matches.append({
            "label": token,
            "box_index": idx,
        })
    return matches


def _parse_serial_number(value: str) -> int:
    """Parse a serial token like sn1234 into an integer for sorting/comparison."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return 0
    try:
        return int(digits)
    except Exception:
        return 0


def _metadata_csv_signature() -> tuple[int, int] | None:
    """Return (mtime_ns, size) for the metadata CSV, or None if it is unreadable."""
    try:
        stat = local_photos.metadata_csv_path().stat()
        return (int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        return None


def _fetch_photo_metadata_rows_live() -> list[list[str]]:
    """Build labeler-compatible photo metadata rows from the local metadata CSV.

    Reads the CSV without taking local_photos._METADATA_FILE_LOCK. Writers there
    rewrite via a temp file plus os.replace, so a torn read is not possible, but
    a write landing mid-read can leave this cache one revision behind; the
    signature check in force_refresh_photo_rows_cache picks it up on the next call.
    """
    path = local_photos.metadata_csv_path()
    if not path.is_file():
        return []
    rows: list[list[str]] = [[
        "CatID",
        "",
        "",
        "",
        "",
        "",
        "Picture Link",
        "Serial Number",
        "Box Coordinates",
        "Box Cat IDs",
        "LabeledBy",
        "Comments",
    ]]
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for raw in reader:
                labels = str((raw or {}).get("Box Cat IDs", "") or "").strip()
                rows.append([
                    labels.replace("|", ", "),
                    "",
                    "",
                    "",
                    "",
                    "",
                    str((raw or {}).get("Discord URL", "") or ""),
                    str((raw or {}).get("Serial Number", "") or ""),
                    str((raw or {}).get("Box Coordinates", "") or ""),
                    labels,
                    str((raw or {}).get("Label Author", "") or ""),
                    str((raw or {}).get("Comments", "") or ""),
                ])
    except Exception:
        return []
    return rows

def _load_photo_metadata_snapshot() -> tuple[list[list[str]] | None, float]:
    """Load the cached photo metadata snapshot from disk."""
    try:
        data = json.loads(_PHOTO_METADATA_SNAPSHOT.read_text(encoding="utf-8"))
        rows = data.get("rows")
        ts = float(data.get("ts") or 0.0)
        if isinstance(rows, list):
            return rows, ts
    except Exception:
        pass
    return None, 0.0

def _write_photo_metadata_snapshot(rows: list[list[str]]) -> None:
    """Write the photo metadata snapshot to disk with current Unix timestamp."""
    try:
        _PHOTO_METADATA_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _PHOTO_METADATA_SNAPSHOT.write_text(json.dumps({"ts": time.time(), "rows": rows}), encoding="utf-8")
    except Exception:
        pass


def _reset_photo_metadata_index() -> None:
    """Invalidate the derived photo metadata lookup index."""
    global _PHOTO_METADATA_INDEX_CACHE, _PHOTO_METADATA_INDEX_TS
    _PHOTO_METADATA_INDEX_CACHE = None
    _PHOTO_METADATA_INDEX_TS = 0.0


def _build_photo_metadata_index(rows: list[list[str]]) -> dict[str, list[list[str]]]:
    """Group rows by normalized cat label for fast show-photo lookups."""
    grouped: dict[str, list[list[str]]] = {}
    for row in rows[1:] if rows else []:
        if len(row) <= COL_SERIAL:
            continue
        for token in _photo_row_label_tokens(row):
            key = norm_alnum_lower(token)
            if not key:
                continue
            grouped.setdefault(key, []).append(row)
    for match_rows in grouped.values():
        match_rows.sort(key=lambda row: _parse_serial_number(row[COL_SERIAL] if len(row) > COL_SERIAL else ""))
    return grouped


def _get_photo_metadata_index(ttl_sec: int | None = None) -> dict[str, list[list[str]]]:
    """Return the cached metadata-row index keyed by normalized cat label."""
    global _PHOTO_METADATA_INDEX_CACHE, _PHOTO_METADATA_INDEX_TS
    rows = get_photo_metadata_rows(ttl_sec)
    if not rows:
        return {}
    rows_ts = float(_PHOTO_METADATA_ROWS_TS)
    if _PHOTO_METADATA_INDEX_CACHE is not None and _PHOTO_METADATA_INDEX_TS == rows_ts:
        return _PHOTO_METADATA_INDEX_CACHE
    index = _build_photo_metadata_index(rows)
    _PHOTO_METADATA_INDEX_CACHE = index
    _PHOTO_METADATA_INDEX_TS = rows_ts
    return index


def _matched_photo_rows(full_name: str, ttl_sec: int | None = None) -> list[list[str]]:
    """Return metadata rows matching a cat label via the cached index."""
    query_key = _photo_query_key(full_name)
    if not query_key:
        return []
    index = _get_photo_metadata_index(ttl_sec)
    return list(index.get(query_key, []))


def _matched_photo_entries(full_name: str, ttl_sec: int | None = None) -> list[dict[str, Any]]:
    """Return matching rows plus the matching box-label position within each row."""
    query_key = _photo_query_key(full_name)
    if not query_key:
        return []
    entries: list[dict[str, Any]] = []
    for row in _matched_photo_rows(full_name, ttl_sec):
        match_details = _photo_row_match_details(row, query_key)
        entries.append({
            "row": row,
            "matched_label": match_details[0]["label"] if match_details else None,
            "matched_box_index": match_details[0]["box_index"] if match_details else None,
            "matched_box_indices": [int(m["box_index"]) for m in match_details],
        })
    return entries

def force_refresh_photo_rows_cache() -> list[list[str]]:
    """Force refresh the local photo metadata row cache, bypassing TTL.

    Skips the work when the metadata CSV has not changed since the cached rows
    were built. Callers that refresh after writing the CSV always see a changed
    signature and still get a full rebuild; the no-op path exists for lookups
    that refresh-and-retry on a miss (a cat with no photos), which would
    otherwise re-read the CSV, rebuild the label index and rewrite the multi-MB
    snapshot on every single request while never finding anything new.
    """
    global _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_TS, _PHOTO_METADATA_ROWS_SIG
    signature = _metadata_csv_signature()
    if (
        _PHOTO_METADATA_ROWS_CACHE is not None
        and _PHOTO_METADATA_ROWS_SIG is not None
        and signature is not None
        and signature == _PHOTO_METADATA_ROWS_SIG
    ):
        return _PHOTO_METADATA_ROWS_CACHE
    try:
        rows = _fetch_photo_metadata_rows_live()
        if not rows:
            return _PHOTO_METADATA_ROWS_CACHE or []
        _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_TS = rows, time.monotonic()
        _PHOTO_METADATA_ROWS_SIG = signature
        _reset_photo_metadata_index()
        _write_photo_metadata_snapshot(rows)
        return rows
    except Exception:
        return _PHOTO_METADATA_ROWS_CACHE or []

def get_photo_metadata_rows(ttl_sec: int | None = None) -> list[list[str]]:
    """Fetch local photo metadata rows with in-memory + on-disk caching."""
    global _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_TS, _PHOTO_METADATA_ROWS_SIG
    ttl = int(
        ttl_sec
        if ttl_sec is not None
        else getattr(settings, "photo_metadata_cache_ttl_sec", 300) or 300
    )
    ttl = max(1, ttl)
    now_mono = time.monotonic()
    now_unix = time.time()
    # In-memory cache (uses monotonic, session-only)
    if _PHOTO_METADATA_ROWS_CACHE is not None and (now_mono - _PHOTO_METADATA_ROWS_TS) < ttl:
        return _PHOTO_METADATA_ROWS_CACHE
    # On-disk snapshot (uses Unix epoch, survives restarts)
    snap_rows, snap_ts = _load_photo_metadata_snapshot()
    if snap_rows is not None and (now_unix - snap_ts) < ttl:
        _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_TS = snap_rows, now_mono
        _PHOTO_METADATA_ROWS_SIG = None
        _reset_photo_metadata_index()
        return snap_rows
    # Fetch live photo metadata
    signature = _metadata_csv_signature()
    try:
        rows = _fetch_photo_metadata_rows_live()
        if not rows:
            return snap_rows or _PHOTO_METADATA_ROWS_CACHE or []
        _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_TS = rows, now_mono
        _PHOTO_METADATA_ROWS_SIG = signature
        _reset_photo_metadata_index()
        _write_photo_metadata_snapshot(rows)
        return rows
    except Exception:
        # Fallback to whatever snapshot we have
        if snap_rows is not None:
            _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_TS = snap_rows, now_mono
            _PHOTO_METADATA_ROWS_SIG = None
            _reset_photo_metadata_index()
            return snap_rows
        return []

async def get_most_recent_photo(full_name: str, _retried: bool = False) -> dict | str:
    """Fetch the most recent photo row for a cat from the long-format list.
    
    If no photos found, force-refresh cache and retry once.
    """
    display_name = _display_label(full_name) or str(full_name or "").strip()
    matches = []

    try:
        entries = _matched_photo_entries(full_name)
    except Exception as e:
        return f"Photo metadata error: {e}"

    for entry in entries:
        r = entry.get("row") or []
        #Safety check for row length
        if len(r) <= COL_SERIAL:
            continue
        serial_num = _parse_serial_number(r[COL_SERIAL] if len(r) > COL_SERIAL else "")
        if serial_num <= 0 or not local_photos.has_local_photo(serial_num):
            continue
        matches.append(entry)

    if not matches:
        #Force refresh and retry once
        if not _retried:
            force_refresh_photo_rows_cache()
            return await get_most_recent_photo(full_name, _retried=True)
        return f"No photos found for {display_name}."

    #Sort by Serial Number (descending)
    def parse_serial(entry):
        row = entry.get("row") or []
        return _parse_serial_number(row[COL_SERIAL] if len(row) > COL_SERIAL else "")

    best_entry = max(matches, key=parse_serial)
    best_row = best_entry.get("row") or []
    
    return {
        "actual_name": display_name,
        "url": best_row[COL_URL] if len(best_row) > COL_URL else "",
        "serial": best_row[COL_SERIAL],
        "total_available": len(matches),
        "matched_label": best_entry.get("matched_label"),
        "matched_box_index": best_entry.get("matched_box_index"),
        "matched_box_indices": best_entry.get("matched_box_indices") or [],
    }

async def get_recent_photo(full_name: str, _retried: bool = False) -> dict | str:
    """Pick a random recent photo from the full history.
    
    If no photos found, force-refresh cache and retry once.
    """
    display_name = _display_label(full_name) or str(full_name or "").strip()
    matches = []

    try:
        entries = _matched_photo_entries(full_name)
    except Exception as e:
        return f"Photo metadata error: {e}"

    for entry in entries:
        r = entry.get("row") or []
        if len(r) <= COL_SERIAL:
            continue
        serial_num = _parse_serial_number(r[COL_SERIAL] if len(r) > COL_SERIAL else "")
        if serial_num <= 0 or not local_photos.has_local_photo(serial_num):
            continue
        matches.append(entry)

    if not matches:
        #Force refresh and retry once
        if not _retried:
            force_refresh_photo_rows_cache()
            return await get_recent_photo(full_name, _retried=True)
        return f"No recent photos for '{display_name}'."

    import random
    
    #Sort by serial ascending (oldest=lowest serial first)
    def parse_serial(entry):
        row = entry.get("row") or []
        return _parse_serial_number(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
    
    matches.sort(key=parse_serial)
    pick_idx = random.randrange(len(matches))
    pick = matches[pick_idx]
    pick_row = pick.get("row") or []
    
    return {
        "actual_name": display_name,
        "url": pick_row[COL_URL] if len(pick_row) > COL_URL else "",
        "serial": pick_row[COL_SERIAL],
        "total_available": len(matches),
        "reverse_index": pick_idx + 1,  #1-based: oldest=1, newest=total
        "matched_label": pick.get("matched_label"),
        "matched_box_index": pick.get("matched_box_index"),
        "matched_box_indices": pick.get("matched_box_indices") or [],
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
    """Return a CatDatabase-backed profile dict or a friendly error string."""
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
    """Build a profile embed from CatDatabase fields and local photo metadata."""
    prof = await get_cat_profile(query)
    if isinstance(prof, str):
        return prof  #error string from get_cat_profile

    # Prefer the most recent photo from local metadata; do not use CatDatabase
    # image fields for runtime photo selection during the local migration.
    recent = await get_most_recent_photo(prof["actual_name"])
    img_url = None
    if isinstance(recent, dict) and recent.get("url"):
        img_url = recent["url"]

    # Match the compact profile card layout used in the cats-on-campus channel.
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
        #Sourced from CatDatabase "Last Seen By", which sync_catabase_photo_columns
        #sets to the author of the most recent photo. Labelling that "Reported"
        #collided with the actual report flow and read as though posting a photo
        #had filed a report on the poster's behalf.
        lines.append("**Last Seen:** " + " ".join(last_bits))

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
