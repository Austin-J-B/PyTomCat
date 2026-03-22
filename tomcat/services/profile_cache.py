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

def _load_from_csv() -> None:
    """Hydrate cache from the bundled CSV fallback."""
    """Build cache from the local CSV snapshot if available."""
    global _CACHE, _TS
    try:
        import csv
        for path in _readable_catabase_csv_paths():
            if not os.path.exists(path):
                continue
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                def hkey(s: str) -> str:
                    return re.sub(r"[^a-z]+", "", (s or '').lower())
                idx = {hkey(h): i for i, h in enumerate(header)}
                def col(*keys: str) -> int:
                    for k in keys:
                        if k in idx: return idx[k]
                    return -1
                i_full = col('fulllegalname','fullname','name','catdatabase','full')
                i_img  = col('imageurl','image','photo','mostrecentimageurl','mostrecentimage','linkofmostrecentimage','linkofmostrecentimageurl')
                i_loc  = col('location')
                i_phys = col('physicaldescription')
                i_beh  = col('behavior')
                i_bday = col('birthdayestimate','birthday')
                i_tnrd = col('tnrd')
                i_tndt = col('tnrdate')
                i_sex  = col('sex')
                i_nick = col('commonnicknames','nicknames')
                i_comm = col('comments','notes')
                i_lsd  = col('lastseendate')
                i_lst  = col('lastseentime')
                i_lsb  = col('lastseenby')
                cache: Dict[str, Dict[str, Any]] = {}
                for r in reader:
                    full_idx = i_full if i_full >= 0 else 0
                    full = (r[full_idx] if full_idx < len(r) else '').strip()
                    if not full:
                        continue
                    disp = _display_from_full(full)
                    key = _norm(disp)
                    cache[key] = {
                        "actual_name": full,
                        "image_url": (r[i_img] if i_img >= 0 and i_img < len(r) else None),
                        "location": (r[i_loc] if i_loc >= 0 and i_loc < len(r) else None),
                        "physical_description": (r[i_phys] if i_phys >= 0 and i_phys < len(r) else None),
                        "behavior": (r[i_beh] if i_beh >= 0 and i_beh < len(r) else None),
                        "birthday_estimate": (r[i_bday] if i_bday >= 0 and i_bday < len(r) else None),
                        "tnrd": (r[i_tnrd] if i_tnrd >= 0 and i_tnrd < len(r) else None),
                        "tnr_date": (r[i_tndt] if i_tndt >= 0 and i_tndt < len(r) else None),
                        "sex": (r[i_sex] if i_sex >= 0 and i_sex < len(r) else None),
                        "nicknames": (r[i_nick] if i_nick >= 0 and i_nick < len(r) else None),
                        "comments": (r[i_comm] if i_comm >= 0 and i_comm < len(r) else None),
                        "last_seen_date": (r[i_lsd] if i_lsd >= 0 and i_lsd < len(r) else None),
                        "last_seen_time": (r[i_lst] if i_lst >= 0 and i_lst < len(r) else None),
                        "last_seen_by": (r[i_lsb] if i_lsb >= 0 and i_lsb < len(r) else None),
                    }
                if cache:
                    _CACHE = cache
                    _TS = time.monotonic()
                    return
    except Exception:
        pass

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
    """Synchronously refresh the cache; returns number of profiles."""
    """Refresh the in-process cache from the CatDatabase sheet. Returns count on success, 0 on failure."""
    sid = getattr(settings, 'sheet_catabase_id', None)
    if not sid:
        return 0
    try:
        ws = sheets_client().open_by_key(sid).worksheet("CatDatabase")
        rows = ws.get_all_values()
    except Exception:
        # Fall back to the snapshot and report that no fresh sheet read occurred.
        _load_snapshot()
        return 0
    if not rows:
        return 0
    try:
        _sync_metadata_names_from_catabase_rows(rows)
    except Exception:
        pass
    header, *data = rows
    #Map known columns by approximate keys
    def hkey(s: str) -> str:
        return re.sub(r"[^a-z]+", "", (s or '').lower())
    idx = {hkey(h): i for i, h in enumerate(header)}
    def col(*keys: str) -> int:
        for k in keys:
            if k in idx: return idx[k]
        return -1
    i_full = col('fulllegalname','fullname','name','catdatabase','full')
    i_img  = col('imageurl','image','photo','mostrecentimageurl','mostrecentimage','linkofmostrecentimage','linkofmostrecentimageurl')
    i_loc  = col('location')
    i_phys = col('physicaldescription')
    i_beh  = col('behavior')
    i_bday = col('birthdayestimate','birthday')
    i_tnrd = col('tnrd')
    i_tndt = col('tnrdate')
    i_sex  = col('sex')
    i_nick = col('commonnicknames','nicknames')
    i_comm = col('comments','notes')
    i_lsd  = col('lastseendate')
    i_lst  = col('lastseentime')
    i_lsb  = col('lastseenby')

    cache: Dict[str, Dict[str, Any]] = {}
    for r in data:
        #Fallback: if we couldn't detect a header for full name, use first column (0)
        full_idx = i_full if i_full >= 0 else 0
        full = (r[full_idx] if full_idx < len(r) else '').strip()
        if not full:
            continue
        disp = _display_from_full(full)
        key = _norm(disp)
        cache[key] = {
            "actual_name": full,
            "image_url": (r[i_img] if i_img >= 0 and i_img < len(r) else None),
            "location": (r[i_loc] if i_loc >= 0 and i_loc < len(r) else None),
            "physical_description": (r[i_phys] if i_phys >= 0 and i_phys < len(r) else None),
            "behavior": (r[i_beh] if i_beh >= 0 and i_beh < len(r) else None),
            "birthday_estimate": (r[i_bday] if i_bday >= 0 and i_bday < len(r) else None),
            "tnrd": (r[i_tnrd] if i_tnrd >= 0 and i_tnrd < len(r) else None),
            "tnr_date": (r[i_tndt] if i_tndt >= 0 and i_tndt < len(r) else None),
            "sex": (r[i_sex] if i_sex >= 0 and i_sex < len(r) else None),
            "nicknames": (r[i_nick] if i_nick >= 0 and i_nick < len(r) else None),
            "comments": (r[i_comm] if i_comm >= 0 and i_comm < len(r) else None),
            "last_seen_date": (r[i_lsd] if i_lsd >= 0 and i_lsd < len(r) else None),
            "last_seen_time": (r[i_lst] if i_lst >= 0 and i_lst < len(r) else None),
            "last_seen_by": (r[i_lsb] if i_lsb >= 0 and i_lsb < len(r) else None),
        }
    if cache:
        global _CACHE, _TS
        _CACHE = cache
        _TS = time.monotonic()
        global _COUNT
        _COUNT = len(_CACHE)
        _save_snapshot()
        #Also write a CSV snapshot with all columns for offline usage
        try:
            path = _preferred_catabase_csv_path()
            with open(path, 'w', encoding='utf-8', newline='') as f:
                w = csv.writer(f)
                for r in rows:
                    w.writerow(r)
        except Exception:
            pass
        return _COUNT
    return 0

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
    global _CACHE, _TS
    if not _CACHE:
        _load_snapshot()
    if not _CACHE:
        _load_from_csv()

def get_profile(name: str) -> Optional[Dict[str, Any]]:
    """Fetch a profile dict from cache, refreshing if stale."""
    _ensure_loaded()
    #Refresh if stale based on TTL
    if _cache_is_stale():
        try:
            refresh_sync()
        except Exception:
            pass
    key = _norm(name)
    if key in _CACHE:
        return _CACHE[key]
    #Try contains search
    for k, v in _CACHE.items():
        if key and key in k:
            return v
    return None

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
