from __future__ import annotations
import os, re, json, asyncio, time
from typing import Optional, Dict, Any, List

from ..config import settings
from .catsheets import sheets_client  # type: ignore

_CACHE: Dict[str, Dict[str, Any]] = {}
_TS: float = 0.0
_COUNT: int = 0

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def _display_from_full(full: str) -> str:
    return re.sub(r"^\s*\d+\.\s*", "", str(full or "")).strip()

def _snapshot_path() -> str:
    base = os.path.join("cache", "catabase")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "profiles.json")

def _load_snapshot() -> None:
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
    """Build cache from the local CSV snapshot if available."""
    global _CACHE, _TS
    try:
        import csv
        path = "Catabase - CatDatabase.csv"
        if not os.path.exists(path):
            return
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return
            def hkey(s: str) -> str:
                return re.sub(r"[^a-z]+", "", (s or '').lower())
            idx = {hkey(h): i for i, h in enumerate(header)}
            def col(*keys: str) -> int:
                for k in keys:
                    if k in idx: return idx[k]
                return -1
            i_full = col('fulllegalname','fullname','name','catdatabase','full')
            i_img  = col('imageurl','image','photo','mostrecentimageurl')
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
    except Exception:
        pass

def _save_snapshot() -> None:
    try:
        with open(_snapshot_path(), 'w', encoding='utf-8') as f:
            json.dump({"ts": time.time(), "profiles": _CACHE}, f)
    except Exception:
        pass

def _ttl_sec() -> int:
    try:
        return int(getattr(settings, 'cat_profile_ttl_sec', 3600) or 3600)
    except Exception:
        return 3600

def refresh_sync() -> int:
    """Refresh the in-process cache from the CatDatabase sheet. Returns count on success, 0 on failure."""
    sid = getattr(settings, 'sheet_catabase_id', None)
    if not sid:
        return 0
    try:
        ws = sheets_client().open_by_key(sid).worksheet("CatDatabase")
        rows = ws.get_all_values()
    except Exception:
        # Attempt snapshot load; still return 0 to indicate no new refresh
        _load_snapshot()
        return 0
    if not rows:
        return 0
    header, *data = rows
    # Map known columns by approximate keys
    def hkey(s: str) -> str:
        return re.sub(r"[^a-z]+", "", (s or '').lower())
    idx = {hkey(h): i for i, h in enumerate(header)}
    def col(*keys: str) -> int:
        for k in keys:
            if k in idx: return idx[k]
        return -1
    i_full = col('fulllegalname','fullname','name','catdatabase','full')
    i_img  = col('imageurl','image','photo','mostrecentimageurl')
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
        # Fallback: if we couldn't detect a header for full name, use first column (0)
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
        # Also write a CSV snapshot with all columns for offline usage
        try:
            import csv
            path = "Catabase - CatDatabase.csv"
            with open(path, 'w', encoding='utf-8', newline='') as f:
                w = csv.writer(f)
                for r in rows:
                    w.writerow(r)
        except Exception:
            pass
        return _COUNT
    return 0

async def refresh_async() -> int:
    return await asyncio.to_thread(refresh_sync)

async def start_profile_cache_scheduler() -> None:
    """Periodically refresh the profile cache based on TTL."""
    while True:
        try:
            await refresh_async()
        except Exception:
            pass
        await asyncio.sleep(_ttl_sec())

def cached_count() -> int:
    return int(_COUNT)

def _ensure_loaded() -> None:
    global _CACHE, _TS
    if not _CACHE:
        _load_snapshot()
    if not _CACHE:
        _load_from_csv()

def get_profile(name: str) -> Optional[Dict[str, Any]]:
    _ensure_loaded()
    # Refresh if stale based on TTL
    if (time.monotonic() - _TS) > _ttl_sec():
        try:
            refresh_sync()
        except Exception:
            pass
    key = _norm(name)
    if key in _CACHE:
        return _CACHE[key]
    # Try contains search
    for k, v in _CACHE.items():
        if key and key in k:
            return v
    return None
