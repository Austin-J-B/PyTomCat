"""Station definitions and aliases backed by a local JSON store."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from datetime import datetime, date

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
STATIONS_PATH = _PACKAGE_ROOT / "cache" / "stations.json"
_DEFAULT_EFFECTIVE = "1970-01-01"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# Seed data taken from prior hardcoded aliases; written once if no file exists.
_SEEDED_STATIONS: List[Dict] = [
    {"name": "Microwave", "aliases": ["microwave", "mike", "mikey", "miker", "micro", "wave", "old man", "michael", "him", "himb", "chemistry", "chemistry building", "chemistry/planetarium building", "planetarium", "planetarium building", "library", "life science building", "library life science building"]},
    {"name": "Snickers", "aliases": ["snickers", "snicks"]},
    {"name": "Business", "aliases": ["business", "coba"]},
    {"name": "The Greens", "aliases": ["the greens", "greens", "green", "grink", "grinks", "center chase", "center chase apartments", "center chase apartments & the greens"]},
    {"name": "HOP", "aliases": ["hop", "pecan", "thwop", "thop", "heights", "hops", "heights on pecan"]},
    {"name": "Lot 50", "aliases": ["lot 50", "lot50", "l50", "lot"]},
    {"name": "Mary Kay & Zen", "aliases": ["mary kay and zen", "mkz", "zen", "mary kay", "mary", "kay", "zen gardens", "zen apartments", "mary kay apartments", "mary kay & zen"]},
    {"name": "West Hall", "aliases": ["west hall", "west", "hall"]},
    {"name": "Maintenance", "aliases": ["maintenance", "maint"]},
    {"name": "Bookstore", "aliases": ["bookstore", "first baptist church", "church", "first baptist"]},
    {"name": "West Campus", "aliases": ["west campus"]},
    {"name": "North Campus", "aliases": ["engineering research building", "erb", "north campus"]},
    {"name": "Centennial Courts", "aliases": ["centennial", "centennial courts"]},
    {"name": "KC Hall", "aliases": ["kc hall", "kc", "kalpana chawla", "kalpana chawla hall"]},
]


def _clean_stations(stations: List[Dict]) -> List[Dict]:
    cleaned: List[Dict] = []
    seen = set()
    for item in stations:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        key = _norm(name)
        if key in seen:
            continue
        seen.add(key)
        aliases_raw = item.get("aliases") or []
        if isinstance(aliases_raw, str):
            aliases_raw = [aliases_raw]
        aliases: List[str] = []
        for a in aliases_raw:
            if not a:
                continue
            na = a.strip()
            if not na:
                continue
            if na.lower() == name.lower():
                continue
            if na not in aliases:
                aliases.append(na)
        cleaned.append({"name": name, "aliases": aliases})
    return cleaned


def _load_versions() -> List[Dict]:
    if STATIONS_PATH.exists():
        try:
            with open(STATIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "versions" in data:
                versions = data.get("versions") or []
            elif isinstance(data, dict) and "stations" in data:
                # Legacy single version
                versions = [{"effective_from": _DEFAULT_EFFECTIVE, "stations": data.get("stations") or []}]
            elif isinstance(data, list):
                # Defensive fallback
                versions = data
            else:
                versions = []
        except Exception:
            versions = []
    else:
        versions = []

    if not versions:
        versions = [{"effective_from": _DEFAULT_EFFECTIVE, "stations": _SEEDED_STATIONS}]
        _save_versions(versions, update_meta=False)

    # Normalize/clean
    for v in versions:
        v["effective_from"] = v.get("effective_from") or _DEFAULT_EFFECTIVE
        v["stations"] = _clean_stations(v.get("stations") or [])
    return versions


def _save_versions(versions: List[Dict], update_meta: bool = True) -> None:
    payload = {
        "versions": versions,
        "meta": {}
    }
    if update_meta:
        payload["meta"]["updated_at"] = _now_iso()
    STATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATIONS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(STATIONS_PATH)


def _resolve_version(target: Optional[date]) -> Dict:
    versions = _load_versions()
    if not target:
        target = date.today()
    # pick latest effective_from <= target
    best = None
    for v in versions:
        try:
            eff = datetime.fromisoformat(str(v.get("effective_from"))).date()
        except Exception:
            continue
        if eff <= target and (best is None or datetime.fromisoformat(str(best.get("effective_from"))).date() < eff):
            best = v
    if best:
        return best
    # fallback earliest
    return sorted(versions, key=lambda x: x.get("effective_from") or _DEFAULT_EFFECTIVE)[0]


def station_versions() -> List[Dict]:
    return sorted(_load_versions(), key=lambda v: v.get("effective_from") or _DEFAULT_EFFECTIVE, reverse=True)


def station_definitions(target: Optional[date | str] = None) -> List[Dict]:
    dt_obj = None
    if isinstance(target, str):
        try:
            dt_obj = datetime.fromisoformat(target).date()
        except Exception:
            dt_obj = None
    elif isinstance(target, date):
        dt_obj = target
    ver = _resolve_version(dt_obj)
    return ver.get("stations") or []


def station_names(target: Optional[date | str] = None) -> List[str]:
    return [item.get("name") or "" for item in station_definitions(target) if item.get("name")]


def station_alias_table(target: Optional[date | str] = None) -> Dict[str, List[str]]:
    table: Dict[str, List[str]] = {}
    for item in station_definitions(target):
        name = item.get("name") or ""
        key = _norm(name)
        aliases = []
        for a in item.get("aliases") or []:
            na = _norm(a)
            if not na or na == key:
                continue
            if na not in aliases:
                aliases.append(na)
        aliases.insert(0, key)
        table[key] = aliases
    return table


def station_display_for(key: str, *, target: Optional[date | str] = None) -> str:
    k = _norm(key)
    for item in station_definitions(target):
        if _norm(item.get("name") or "") == k:
            return item.get("name") or key
    return key


def save_stations_version(stations: List[Dict], effective_from: str) -> List[Dict]:
    """Upsert a station version and return sorted versions."""
    cleaned = _clean_stations(stations)
    try:
        eff_date = datetime.fromisoformat(str(effective_from)).date().isoformat()
    except Exception:
        eff_date = _DEFAULT_EFFECTIVE
    versions = _load_versions()
    replaced = False
    for v in versions:
        if str(v.get("effective_from")) == eff_date:
            v["stations"] = cleaned
            replaced = True
            break
    if not replaced:
        versions.append({"effective_from": eff_date, "stations": cleaned})
    versions = sorted(versions, key=lambda v: v.get("effective_from") or _DEFAULT_EFFECTIVE)
    _save_versions(versions, update_meta=True)
    return versions
