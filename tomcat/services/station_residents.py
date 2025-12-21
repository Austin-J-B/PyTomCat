"""Cache-backed helpers for mapping feeding stations to resident cats."""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..aliases import resolve_station_or_cat
from ..config import settings

try:
    from .sheets_client import sheets_client
except Exception:  #pragma: no cover - during tests without Sheets client
    sheets_client = None  #type: ignore

_CACHE: Dict[str, List[str]] = {}
_CACHE_TS: float = 0.0
_CACHE_TTL_SEC = 15 * 60

_FALLBACK_PATHS = [Path("Catabase - CatDatabase.csv")]

_LOCATION_HEADERS = {"location", "preferred location", "station"}

_RE_WHITESPACE = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    return _RE_WHITESPACE.sub(" ", (value or "").strip())


def _display_name(full: str) -> str:
    full = (full or "").strip()
    match = re.match(r"\s*\d+[\.|\s]+(.+)$", full)
    return match.group(1).strip() if match else full


def _split_locations(raw: str) -> List[str]:
    if not raw:
        return []
    sanitized = raw.replace("&", ",").replace("/", ",").replace(";", ",")
    sanitized = re.sub(r"\band\b", ",", sanitized, flags=re.I)
    parts = [p.strip(" \t\r\n.!?") for p in sanitized.split(",")]
    out: List[str] = []
    for part in parts:
        if not part:
            continue
        #Decompose multi-word pieces like "center chase apartments the greens"
        if "  " in part:
            part = _RE_WHITESPACE.sub(" ", part)
        if part:
            out.append(part)
    return out


def _load_rows_from_sheet() -> Optional[List[List[str]]]:
    if not settings.sheet_catabase_id or sheets_client is None:
        return None
    try:
        gc = sheets_client()
        ws = gc.open_by_key(settings.sheet_catabase_id).worksheet("CatDatabase")
        rows = ws.get_all_values()
        return rows or None
    except Exception:
        return None


def _load_rows_from_csv() -> Optional[List[List[str]]]:
    for path in _FALLBACK_PATHS:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                rows = list(reader)
                if rows:
                    return rows
        except Exception:
            continue
    return None


def _determine_location_index(header: List[str]) -> Optional[int]:
    lowered = [str(h or "").strip().lower() for h in header]
    for idx, name in enumerate(lowered):
        if name in _LOCATION_HEADERS:
            return idx
    return None


def _build_mapping(rows: List[List[str]]) -> Dict[str, List[str]]:
    if not rows:
        return {}
    header = rows[0]
    data = rows[1:]
    loc_idx = _determine_location_index(header)
    if loc_idx is None:
        return {}

    mapping: Dict[str, List[str]] = {}
    for row in data:
        if len(row) <= loc_idx:
            continue
        display = _display_name(row[0] if row else "")
        if not display:
            continue
        raw_loc = _normalize_text(row[loc_idx])
        if not raw_loc:
            continue
        segments = _split_locations(raw_loc)
        if not segments:
            continue
        seen: set[str] = set()
        for segment in segments:
            station_name = resolve_station_or_cat(segment, want="station", include_stopword_aliases=True)
            if not station_name:
                #Try again with "station" removed (e.g., "Lot 50 station")
                cleaned = re.sub(r"\bstations?\b", "", segment, flags=re.I)
                cleaned = _normalize_text(cleaned)
                if cleaned and cleaned != segment:
                    station_name = resolve_station_or_cat(cleaned, want="station", include_stopword_aliases=True)
            if not station_name:
                continue
            key = station_name
            if key not in mapping:
                mapping[key] = []
            if display not in mapping[key] and key not in seen:
                mapping[key].append(display)
            seen.add(key)
    for cats in mapping.values():
        cats.sort(key=lambda name: name.lower())
    return mapping


def _ensure_cache(force: bool = False) -> Dict[str, List[str]]:
    global _CACHE, _CACHE_TS, _CACHE_SOURCE
    now = time.monotonic()
    if not force and _CACHE and (now - _CACHE_TS) < _CACHE_TTL_SEC:
        return _CACHE

    rows = _load_rows_from_sheet()
    if rows is None:
        rows = _load_rows_from_csv()

    mapping = _build_mapping(rows or [])
    _CACHE = mapping
    _CACHE_TS = now
    return mapping


def get_residents_for_station(station: str, *, force_refresh: bool = False) -> List[str]:
    if not station:
        return []
    mapping = _ensure_cache(force=force_refresh)
    return mapping.get(station, [])


def refresh_resident_cache() -> None:
    _ensure_cache(force=True)
