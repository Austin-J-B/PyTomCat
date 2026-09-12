"""Background refresh + access layer for cat profile cache files."""

from __future__ import annotations
import os, re, json, asyncio, time, csv
from typing import Optional, Dict, Any, List

from ..config import settings
from ..logger import log_action
from .catsheets import sheets_client  #type: ignore
from . import local_photos

_CACHE: Dict[str, Dict[str, Any]] = {}
_TS: float = 0.0
_COUNT: int = 0
_CAT_ID_RE = re.compile(r"^\s*(\d+)\s*[.)\-:]?\s*(.*?)\s*$")
_CATABASE_CSV_PATH = os.path.join("cache", "catabase", "Catabase - CatDatabase.csv")
_LEGACY_CATABASE_CSV_PATH = "Catabase - CatDatabase.csv"


def _readable_catabase_csv_paths() -> list[str]:
    return [_CATABASE_CSV_PATH, _LEGACY_CATABASE_CSV_PATH]


def _preferred_catabase_csv_path() -> str:
    os.makedirs(os.path.join("cache", "catabase"), exist_ok=True)
    return _CATABASE_CSV_PATH

def _norm(s: str) -> str:
    """Normalize strings for case-insensitive lookup."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def _display_from_full(full: str) -> str:
    """Convert spreadsheet full name into display form."""
    return re.sub(r"^\s*\d+\.\s*", "", str(full or "")).strip()


def _parse_cat_id_and_display(full: str) -> tuple[str | None, str]:
    text = str(full or "").strip()
    if not text:
        return None, ""
    match = _CAT_ID_RE.match(text)
    if match:
        return str(match.group(1)).strip(), str(match.group(2) or "").strip()
    return None, text


def _id_name_map_from_sheet_rows(rows: list[list[str]]) -> Dict[str, str]:
    if not rows:
        return {}
    out: Dict[str, str] = {}
    for row in rows[1:]:
        full = (row[0] if row else "").strip()
        cat_id, display = _parse_cat_id_and_display(full)
        if cat_id and display:
            out[cat_id] = display
    return out


def _id_name_map_from_local_snapshot() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for path in _readable_catabase_csv_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    full = (row[0] if row else "").strip()
                    cat_id, display = _parse_cat_id_and_display(full)
                    if cat_id and display:
                        out[cat_id] = display
            if out:
                return out
        except Exception:
            continue
    return out


def _sync_metadata_names_from_catabase_rows(rows: list[list[str]]) -> dict[str, Any]:
    previous = _id_name_map_from_local_snapshot()
    current = _id_name_map_from_sheet_rows(rows)
    if not previous or not current:
        return {"status": "no_baseline", "rename_pairs": 0, "rows_changed": 0, "labels_replaced": 0}
    rename_map: Dict[str, str] = {}
    for cat_id, new_name in current.items():
        old_name = previous.get(cat_id)
        if not old_name:
            continue
        if str(old_name).strip() == str(new_name).strip():
            continue
        rename_map[str(old_name)] = str(new_name)
    result = local_photos.rename_metadata_cat_labels(rename_map)
    if int(result.get("rows_changed", 0) or 0) > 0:
        log_action(
            "catabase_name_detect",
            f"pairs={int(result.get('rename_pairs', 0) or 0)}",
            f"rows={int(result.get('rows_changed', 0) or 0)}; labels={int(result.get('labels_replaced', 0) or 0)}",
        )
    return result

def _snapshot_path() -> str:
    """Return local path for the cached profile snapshot."""
    base = os.path.join("cache", "catabase")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "profiles.json")

def _load_snapshot() -> None:
    """Load the on-disk snapshot if present."""
    global _CACHE, _TS
    try:
        with open(_snapshot_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        _CACHE = {str(k): v for k, v in (data.get('profiles') or {}).items()}
        _TS = float(data.get('ts') or 0.0)
    except Exception:
        _CACHE = {}
        _TS = 0.0

#Profile field -> the header spellings the CatDatabase has used for it, most
#specific first. Column order has changed over the sheet's life, so columns are
#found by header rather than position, and a missing one reads as None.
_PROFILE_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("image_url", ("imageurl", "image", "photo", "mostrecentimageurl",
                   "mostrecentimage", "linkofmostrecentimage",
                   "linkofmostrecentimageurl")),
    ("location", ("location",)),
    ("physical_description", ("physicaldescription",)),
    ("behavior", ("behavior",)),
    ("birthday_estimate", ("birthdayestimate", "birthday")),
    ("tnrd", ("tnrd",)),
    ("tnr_date", ("tnrdate",)),
    ("sex", ("sex",)),
    ("nicknames", ("commonnicknames", "nicknames")),
    ("comments", ("comments", "notes")),
    ("last_seen_date", ("lastseendate",)),
    ("last_seen_time", ("lastseentime",)),
    ("last_seen_by", ("lastseenby",)),
)
_FULL_NAME_HEADERS = ("fulllegalname", "fullname", "name", "catdatabase", "full")


def _header_key(text: str) -> str:
    return re.sub(r"[^a-z]+", "", (text or "").lower())


def _profiles_from_rows(rows: List[List[str]]) -> Dict[str, Dict[str, Any]]:
    """Cat profiles keyed by normalized name, from a CatDatabase table.

    rows[0] is the header. The live sheet and the CSV snapshot share this
    layout, so both go through here.
    """
    if not rows:
        return {}
    header, *data = rows
    index = {_header_key(h): i for i, h in enumerate(header)}

    def column(names: tuple[str, ...]) -> int:
        for name in names:
            if name in index:
                return index[name]
        return -1

    #With no recognizable name header, assume the first column.
    full_col = max(column(_FULL_NAME_HEADERS), 0)
    columns = [(field, column(names)) for field, names in _PROFILE_COLUMNS]

    profiles: Dict[str, Dict[str, Any]] = {}
    for row in data:
        full = (row[full_col] if full_col < len(row) else "").strip()
        if not full:
            continue
        profile: Dict[str, Any] = {"actual_name": full}
        for field, col in columns:
            profile[field] = row[col] if 0 <= col < len(row) else None
        profiles[_norm(_display_from_full(full))] = profile
    return profiles


def _load_from_csv() -> None:
    """Hydrate the cache from the bundled CSV snapshot, if one is readable."""
    global _CACHE, _TS
    for path in _readable_catabase_csv_paths():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                profiles = _profiles_from_rows(list(csv.reader(handle)))
        except Exception:
            continue
        if profiles:
            _CACHE = profiles
            _TS = time.monotonic()
            return


def _save_snapshot() -> None:
    """Persist the current cache to disk for restarts."""
    try:
        with open(_snapshot_path(), 'w', encoding='utf-8') as f:
            json.dump({"ts": time.time(), "profiles": _CACHE}, f)
    except Exception:
        pass

def _ttl_sec() -> int:
    """Return how long the cache stays fresh before a refresh."""
    try:
        return int(getattr(settings, 'cat_profile_ttl_sec', 3600) or 3600)
    except Exception:
        return 3600

def _cache_is_stale() -> bool:
    """Check staleness for either monotonic or wall-clock timestamps."""
    ts = float(_TS or 0.0)
    ttl = float(_ttl_sec())
    if ts <= 0.0:
        return True
    # Snapshot files store epoch seconds; in-memory refresh uses monotonic seconds.
    if ts > 1_000_000_000:
        return (time.time() - ts) > ttl
    return (time.monotonic() - ts) > ttl

def refresh_sync() -> int:
    """Refresh the cache from the CatDatabase sheet. Returns the count, 0 on failure."""
    global _CACHE, _TS, _COUNT
    sid = getattr(settings, 'sheet_catabase_id', None)
    if not sid:
        return 0
    try:
        rows = sheets_client().open_by_key(sid).worksheet("CatDatabase").get_all_values()
    except Exception:
        #Fall back to the snapshot and report that no fresh sheet read occurred.
        _load_snapshot()
        return 0
    if not rows:
        return 0
    try:
        _sync_metadata_names_from_catabase_rows(rows)
    except Exception:
        pass

    profiles = _profiles_from_rows(rows)
    if not profiles:
        return 0
    _CACHE = profiles
    _TS = time.monotonic()
    _COUNT = len(_CACHE)
    _save_snapshot()
    #Also keep an all-columns CSV snapshot for offline use.
    try:
        with open(_preferred_catabase_csv_path(), 'w', encoding='utf-8', newline='') as f:
            csv.writer(f).writerows(rows)
    except Exception:
        pass
    return _COUNT


async def refresh_async() -> int:
    """Async wrapper that runs refresh_sync off the event loop."""
    return await asyncio.to_thread(refresh_sync)

async def start_profile_cache_scheduler() -> None:
    """Loop that periodically refreshes the profile snapshot."""
    """Periodically refresh the profile cache based on TTL."""
    while True:
        try:
            await refresh_async()
        except Exception:
            pass
        await asyncio.sleep(_ttl_sec())

def cached_count() -> int:
    """Return how many profiles are currently cached."""
    return int(_COUNT)

def all_actual_names() -> list[str]:
    """Return a list of full cat names (with numeric prefixes) from cache."""
    _ensure_loaded()
    if _cache_is_stale():
        try:
            refresh_sync()
        except Exception:
            pass
    if not _CACHE:
        return []
    names: list[str] = []
    for entry in _CACHE.values():
        full = entry.get("actual_name")
        if full:
            names.append(str(full))
    return names

def _ensure_loaded() -> None:
    """Lazy-load the cache if nothing has been loaded yet."""
    if not _CACHE:
        _load_snapshot()
    if not _CACHE:
        _load_from_csv()

def get_profile_exact(name: str) -> Optional[Dict[str, Any]]:
    """Exact normalized-name lookup from local cache only (no sheet refresh, no substring match).

    Freshness comes from start_profile_cache_scheduler; misses should fall back to a live read.
    """
    _ensure_loaded()
    key = _norm(_display_from_full(name))
    if not key:
        return None
    return _CACHE.get(key)

def get_profile_local(name: str) -> Optional[Dict[str, Any]]:
    """Fetch a profile dict from local cache only (no sheet refresh)."""
    _ensure_loaded()
    key = _norm(name)
    if key in _CACHE:
        return _CACHE[key]
    for k, v in _CACHE.items():
        if key and key in k:
            return v
    return None
