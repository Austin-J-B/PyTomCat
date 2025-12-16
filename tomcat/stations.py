"""Station definitions and aliases backed by a local JSON store."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

from datetime import datetime

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
STATIONS_PATH = _PACKAGE_ROOT / "cache" / "stations.json"


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


def _load_raw() -> dict:
    if STATIONS_PATH.exists():
        try:
            with open(STATIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["stations"] = data.get("stations") or []
                data["meta"] = data.get("meta") or {}
                return data
        except Exception:
            pass
    # Seed new file
    seeded = {"stations": _SEEDED_STATIONS, "meta": {"seeded_at": _now_iso()}}
    STATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATIONS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seeded, f, indent=2)
    tmp.replace(STATIONS_PATH)
    return seeded


def save_stations(stations: List[Dict], update_meta: bool = True) -> None:
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

    try:
        current_meta = _load_raw().get("meta", {})
    except Exception:
        current_meta = {}

    payload = {"stations": cleaned, "meta": current_meta}
    if update_meta:
        payload["meta"]["updated_at"] = _now_iso()

    STATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATIONS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(STATIONS_PATH)


def station_definitions() -> List[Dict]:
    return _load_raw().get("stations", [])


def station_names() -> List[str]:
    return [item.get("name") or "" for item in station_definitions() if item.get("name")]


def station_alias_table() -> Dict[str, List[str]]:
    table: Dict[str, List[str]] = {}
    for item in station_definitions():
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


def station_display_for(key: str) -> str:
    k = _norm(key)
    for item in station_definitions():
        if _norm(item.get("name") or "") == k:
            return item.get("name") or key
    return key
