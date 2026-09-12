"""Station definitions and aliases backed by a local JSON store."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from datetime import datetime, date

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
STATIONS_PATH = _PACKAGE_ROOT / "cache" / "stations.ndjson"
STATIONS_LEGACY_PATH = _PACKAGE_ROOT / "cache" / "stations.json"
_DEFAULT_EFFECTIVE = "1970-01-01"


def _now_iso() -> str:
    return datetime.now().isoformat()


_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip().lower())


#Seed data taken from prior hardcoded aliases; written once if no file exists.
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


_VERSIONS_CACHE: Optional[List[Dict]] = None
_VERSIONS_CACHE_MTIME: Optional[float] = None
#Bumped whenever _load_versions reparses the file. Derived caches below key off
#it so they rebuild exactly once per station-definition change.
_VERSIONS_GENERATION: int = 0


def station_generation() -> int:
    """Opaque token that changes whenever station definitions are reloaded."""
    _load_versions()
    return _VERSIONS_GENERATION


def _load_versions() -> List[Dict]:
    # Cache parsed versions keyed on the stations file mtime. This function is
    # reached from the hot path (_canonical_station -> station_alias_table ->
    # _resolve_version, called per-station in loops like build_morning_message
    # and on every message that resolves a station name). Re-reading + JSON-
    # parsing the file on every call was hundreds of synchronous reads on the
    # event loop — cheap normally, but seconds of stall when the host is
    # swapping. Now we only re-parse when the file actually changes.
    global _VERSIONS_CACHE, _VERSIONS_CACHE_MTIME, _VERSIONS_GENERATION
    try:
        _mtime = STATIONS_PATH.stat().st_mtime if STATIONS_PATH.exists() else None
    except OSError:
        _mtime = None
    if _VERSIONS_CACHE is not None and _VERSIONS_CACHE_MTIME == _mtime:
        return _VERSIONS_CACHE

    def _read_ndjson(path: Path) -> List[Dict]:
        out: List[Dict] = []
        if not path.exists():
            return out
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("effective_from"):
                    out.append({
                        "effective_from": obj.get("effective_from"),
                        "stations": obj.get("stations") or []
                    })
        except Exception:
            return out
        return out

    versions = _read_ndjson(STATIONS_PATH)
    if not versions and STATIONS_LEGACY_PATH.exists():
        try:
            with open(STATIONS_LEGACY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "versions" in data:
                versions = data.get("versions") or []
            elif isinstance(data, dict) and "stations" in data:
                #Legacy single version
                versions = [{"effective_from": _DEFAULT_EFFECTIVE, "stations": data.get("stations") or []}]
            elif isinstance(data, list):
                #Defensive fallback
                versions = data
            else:
                versions = []
            if versions:
                _save_versions(versions, update_meta=True)
        except Exception:
            versions = []

    if not versions:
        versions = [{"effective_from": _DEFAULT_EFFECTIVE, "stations": _SEEDED_STATIONS}]
        _save_versions(versions, update_meta=False)

    #Normalize/clean
    for v in versions:
        v["effective_from"] = v.get("effective_from") or _DEFAULT_EFFECTIVE
        v["stations"] = _clean_stations(v.get("stations") or [])

    _VERSIONS_CACHE = versions
    _VERSIONS_CACHE_MTIME = _mtime
    _VERSIONS_GENERATION += 1
    return versions


def _save_versions(versions: List[Dict], update_meta: bool = True) -> None:
    meta = {}
    if update_meta:
        meta["updated_at"] = _now_iso()
    STATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATIONS_PATH.with_name(STATIONS_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for v in versions:
            f.write(json.dumps(v, separators=(",", ":")) + "\n")
        f.write(json.dumps({"meta": meta}, separators=(",", ":")) + "\n")
    tmp.replace(STATIONS_PATH)


def _resolve_version(target: Optional[date]) -> Dict:
    versions = _load_versions()
    if not target:
        target = date.today()
    #pick latest effective_from <= target
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
    #fallback earliest
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


def _build_alias_table(items: List[Dict]) -> Dict[str, List[str]]:
    items = list(items)
    seen_names = {_norm(item.get("name") or "") for item in items if item.get("name")}
    # Backfill stations that may be missing from a stale cache version.
    for item in _SEEDED_STATIONS:
        key = _norm(item.get("name") or "")
        if key and key not in seen_names:
            items.append({"name": item.get("name"), "aliases": item.get("aliases") or []})
            seen_names.add(key)
    table: Dict[str, List[str]] = {}
    for item in items:
        key = _norm(item.get("name") or "")
        aliases = dict.fromkeys(
            na for na in (_norm(a) for a in item.get("aliases") or []) if na and na != key
        )
        table[key] = [key, *aliases]
    return table


def _build_display_map(items: List[Dict]) -> Dict[str, str]:
    #Later entries must not shadow earlier ones: the old linear scan returned
    #the first match in definitions, then fell back to the seed list.
    display: Dict[str, str] = {}
    for item in list(items) + _SEEDED_STATIONS:
        name = item.get("name") or ""
        display.setdefault(_norm(name), name)
    return display


#Derived views of the current station version, rebuilt only when it changes.
#Both are read once per resolved station name, so the old per-call rebuild and
#linear scan showed up as hundreds of redundant passes per Discord message.
_DERIVED_CACHE: Dict[Optional[str], tuple] = {}


def _derived(target: Optional[date | str]) -> tuple[Dict[str, List[str]], Dict[str, str]]:
    gen = station_generation()
    ckey = target.isoformat() if isinstance(target, date) else target
    cached = _DERIVED_CACHE.get(ckey)
    if cached is not None and cached[0] == gen:
        return cached[1], cached[2]
    items = station_definitions(target)
    built = (gen, _build_alias_table(items), _build_display_map(items))
    if len(_DERIVED_CACHE) > 32:
        _DERIVED_CACHE.clear()
    _DERIVED_CACHE[ckey] = built
    return built[1], built[2]


def station_alias_table(target: Optional[date | str] = None) -> Dict[str, List[str]]:
    return _derived(target)[0]


def station_display_for(key: str, *, target: Optional[date | str] = None) -> str:
    k = _norm(key)
    return _derived(target)[1].get(k, key)


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
