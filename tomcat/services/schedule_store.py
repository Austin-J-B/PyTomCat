"""The feeding schedule's on-disk store: versioned ndjson, read through a cache.

One schedule file, two readers — the web UI in tomcat/main.py and the bot in
tomcat/handlers/feeding.py — which used to carry their own copies of the parse,
load, save and version-pick logic. Both now come through here, so there is one
place that knows the file format and one cache in front of it.

Each line is a version: ``{"effective_from": "YYYY-MM-DD", "schedule": {...},
"meta": {...}}``, plus a trailing metadata line the readers ignore. A version
applies from its ``effective_from`` until a later one takes over.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..stations import station_names

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_PATH = _PACKAGE_ROOT / "cache" / "feeding_schedule.ndjson"
LEGACY_SCHEDULE_PATH = _PACKAGE_ROOT / "cache" / "feeding_schedule.json"
DEFAULT_EFFECTIVE = "1970-01-01"

#Parsed versions, keyed on the file's mtime and size. Reads come from the web UI
#per request and from the router once per feeding message, and re-parsing the
#whole file each time was synchronous work on the event loop for a file that
#changes a few times a week.
_CACHE: Optional[List[dict]] = None
_CACHE_STAMP: Optional[tuple] = None
_CACHE_CHECKED_MONO: float = float("-inf")
_STAT_INTERVAL_SEC = 2.0


def _read_ndjson(path: Path) -> List[dict]:
    versions: List[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return versions
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("effective_from"):
            versions.append({
                "effective_from": obj.get("effective_from"),
                "schedule": obj.get("schedule") or {},
                "meta": obj.get("meta") or {},
            })
    return versions


def _load_legacy() -> List[dict]:
    """Migrate the pre-ndjson formats forward, once."""
    try:
        data = json.loads(LEGACY_SCHEDULE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and "versions" in data:
        versions = data.get("versions") or []
    elif isinstance(data, dict) and "schedule" in data:
        versions = [{
            "effective_from": DEFAULT_EFFECTIVE,
            "schedule": data.get("schedule") or {},
            "meta": data.get("meta") or {},
        }]
    elif isinstance(data, list):
        versions = data
    else:
        versions = []
    if versions:
        save_versions(versions)
    return versions


def load_versions() -> List[dict]:
    """Every stored schedule version, in file order. Do not mutate the result."""
    global _CACHE, _CACHE_STAMP, _CACHE_CHECKED_MONO
    now = time.monotonic()
    if _CACHE is not None and (now - _CACHE_CHECKED_MONO) < _STAT_INTERVAL_SEC:
        return _CACHE
    try:
        stat = SCHEDULE_PATH.stat()
        stamp: Optional[tuple] = (stat.st_mtime, stat.st_size)
    except OSError:
        stamp = None
    _CACHE_CHECKED_MONO = now
    if _CACHE is not None and _CACHE_STAMP == stamp:
        return _CACHE

    versions = _read_ndjson(SCHEDULE_PATH) if stamp is not None else []
    if not versions:
        #_load_legacy writes through save_versions, which resets these globals.
        versions = _load_legacy()
        _CACHE_CHECKED_MONO = time.monotonic()
        try:
            stat = SCHEDULE_PATH.stat()
            stamp = (stat.st_mtime, stat.st_size)
        except OSError:
            stamp = None
    _CACHE, _CACHE_STAMP = versions, stamp
    return versions


def save_versions(versions: List[dict]) -> None:
    """Rewrite the store atomically and drop the cache."""
    global _CACHE, _CACHE_STAMP
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_PATH.with_name(SCHEDULE_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for version in versions:
            handle.write(json.dumps(version, separators=(",", ":")) + "\n")
        handle.write(json.dumps({"meta": {"updated_at": int(time.time())}},
                                separators=(",", ":")) + "\n")
    tmp.replace(SCHEDULE_PATH)
    _CACHE, _CACHE_STAMP = None, None


def _effective_date(version: dict) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(version.get("effective_from") or DEFAULT_EFFECTIVE)).date()
    except ValueError:
        return None


def version_for_date(target: date) -> Optional[dict]:
    """The latest version effective on or before `target`, else the earliest."""
    versions = load_versions()
    if not versions:
        return None
    best: Optional[dict] = None
    best_eff: Optional[date] = None
    for version in versions:
        eff = _effective_date(version)
        if eff is None or eff > target:
            continue
        if best_eff is None or best_eff < eff:
            best, best_eff = version, eff
    if best is not None:
        return best
    return min(versions, key=lambda v: v.get("effective_from") or DEFAULT_EFFECTIVE)


def resolve_for_date(target: date) -> Dict[str, Any]:
    """The schedule in force on `target`, limited to that week's known stations.

    Callers pass the date themselves: the web UI resolves "today" in server
    local time and the bot resolves it in Central, and those have always
    differed.
    """
    best = version_for_date(target)
    if best is None:
        return {"schedule": {}, "effective_from": DEFAULT_EFFECTIVE, "meta": {}}
    schedule = best.get("schedule")
    if not isinstance(schedule, dict):
        schedule = {}
    allowed = set(station_names(best.get("effective_from")))
    return {
        "schedule": {name: row for name, row in schedule.items() if name in allowed},
        "effective_from": best.get("effective_from") or DEFAULT_EFFECTIVE,
        "meta": best.get("meta") or {},
    }


def upsert_version(schedule: dict, effective_from: str, meta: Optional[dict] = None) -> List[dict]:
    """Replace or add the version for `effective_from` and persist the store."""
    try:
        eff = datetime.fromisoformat(str(effective_from)).date().isoformat()
    except ValueError:
        eff = DEFAULT_EFFECTIVE
    versions = [dict(v) for v in load_versions()]
    for version in versions:
        if str(version.get("effective_from")) == eff:
            version["schedule"] = schedule
            version["meta"] = meta or version.get("meta") or {}
            break
    else:
        versions.append({"effective_from": eff, "schedule": schedule, "meta": meta or {}})
    versions.sort(key=lambda v: v.get("effective_from") or DEFAULT_EFFECTIVE)
    save_versions(versions)
    return versions
