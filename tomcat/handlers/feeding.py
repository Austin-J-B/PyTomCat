"""Feeding log handlers: subs, checklist updates, and reminder workflow."""

#tomcat/feeding.py
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord
from gspread.exceptions import APIError

from ..config import settings
from ..logger import log_action
from ..services.sheets_client import sheets_client
from ..aliases import resolve_station_or_cat
from ..stations import station_names
from ..utils.sender import safe_send

#Optional TZ support
try:
    from zoneinfo import ZoneInfo  #py>=3.9
except Exception:
    ZoneInfo = None  #type: ignore

CENTRAL_TZ = ZoneInfo("America/Chicago") if ZoneInfo else None

#------------- subs log -------------
#Monthly JSONL files under logs/subs/<year>/<year-month>.jsonl
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SUBS_ROOT = _PACKAGE_ROOT / "logs" / "subs"
SUBS_ROOT.mkdir(parents=True, exist_ok=True)
SUBS_LEGACY_FILE = SUBS_ROOT / "subs.jsonl"
_SUBS_LOCK = asyncio.Lock()

# UI-provided schedule cache (ndjson primary, legacy JSON fallback)
UI_SCHEDULE_PATH = _PACKAGE_ROOT / "cache" / "feeding_schedule.ndjson"
UI_SCHEDULE_PATH_LEGACY = _PACKAGE_ROOT / "cache" / "feeding_schedule.json"
_DEFAULT_SCHED_EFFECTIVE = "1970-01-01"
FEEDING_CHECKLIST_PATH = _PACKAGE_ROOT / "cache" / "feeding_checklist.json"
_FEEDING_CHECKLIST_LOCK = asyncio.Lock()

#------------- simple data types ----------------
@dataclass
class SubRecord:
    id: str
    station: str
    dates: List[str]
    requester: int
    assignee: Optional[int]
    status: str  #"requested" | "accepted" | "declined"
    channel_id: int
    message_id: int
    created_at: str

#------------- helpers: time/date ---------------
def _today_iso() -> str:
    """Return today's date string using the configured timezone."""
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    return now.date().isoformat()

def _now_iso() -> str:
    """Return an ISO timestamp with timezone awareness if available."""
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    return now.isoformat()

#------------- helpers: files/json --------------
def _load_json(path: str, default):
    """Read a JSON file, returning default on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(path)

def _sub_month_key_from_datetime(dt: datetime) -> str:
    """Convert a datetime into the YYYY-MM month key."""
    return f"{dt.year}-{dt.month:02d}"


def _sub_month_key_from_date(date_iso: str) -> Optional[str]:
    """Parse an ISO date string into the subs month key."""
    try:
        dt = datetime.fromisoformat(date_iso)
        return _sub_month_key_from_datetime(dt)
    except Exception:
        return None


def _sub_log_path_from_key(key: str) -> str:
    """Return the jsonl log path for a month key, creating folders."""
    try:
        year_str, month_str = key.split("-", 1)
        year = int(year_str)
        month = int(month_str)
    except Exception:
        raise ValueError(f"Invalid sub log month key: {key}")
    folder = SUBS_ROOT / f"{year}"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / f"{year}-{month:02d}.jsonl")


def _recent_month_keys(span: int = 2) -> List[str]:
    """Return month keys for the current month and previous span-1 months."""
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    keys: List[str] = []
    year, month = now.year, now.month
    for offset in range(span):
        y = year
        m = month - offset
        while m <= 0:
            m += 12
            y -= 1
        keys.append(f"{y}-{m:02d}")
    return keys


def _all_sub_month_keys() -> List[str]:
    """List every month that currently has a subs log."""
    keys: set[str] = set()
    if SUBS_ROOT.exists():
        for year_path in SUBS_ROOT.iterdir():
            if not year_path.is_dir():
                continue
            for fname in year_path.iterdir():
                if fname.suffix == ".jsonl":
                    keys.add(fname.stem)
    if SUBS_LEGACY_FILE.exists():
        keys.add("legacy")
    return sorted(keys)


def _read_sub_file(path: str, month_key: Optional[str]) -> List[dict]:
    """Read a monthly subs jsonl and return parsed records."""
    out: List[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if month_key and not row.get("log_month"):
                row["log_month"] = month_key
            if row.get("station"):
                row["station"] = _canonical_station(row.get("station")) or row.get("station")
            if row.get("dates"):
                row["dates"] = _normalize_dates(row.get("dates"))
            out.append(row)
    return out


def _write_sub_file(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    os.replace(tmp_path, path)


def _message_preview(text: Optional[str], *, max_len: int = 200) -> str:
    if not text:
        return ""
    cleaned = str(text).replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1] + "…"


def _append_sub_record(record: dict, month_key: Optional[str] = None) -> None:
    if not month_key:
        month_key = record.get("log_month")
    if not month_key:
        month_key = _derive_month_key(record)
    record["log_month"] = month_key
    path = _sub_log_path_from_key(month_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _normalize_dates(dates: Iterable[str]) -> List[str]:
    """Normalize incoming date strings; UI should send ISO YYYY-MM-DD."""
    out: List[str] = []
    for raw in dates or []:
        if not raw:
            continue
        if isinstance(raw, str):
            iso = _parse_date_str(raw)
            if not iso:
                try:
                    iso = datetime.fromisoformat(raw.replace('Z', '+00:00')).date().isoformat()
                except Exception:
                    iso = None
        else:
            iso = None
            try:
                iso = datetime.fromisoformat(str(raw)).date().isoformat()
            except Exception:
                pass
        if iso:
            out.append(iso)
    return sorted(set(out))

def _normalize_channel_id(val: Any) -> Optional[int]:
    """Best-effort cast of a channel identifier into an int."""
    try:
        v = int(str(val).strip())
        return v if v else None
    except Exception:
        return None

def _resolve_allowed_feeding_channels(explicit: Iterable[Any], ch_feeding_team: Any, ch_sandbox: Any) -> set[int]:
    """
    Normalize the configured allowed feeding channel ids.
    If any explicit values are configured, always fold in the main feeding channel
    (and sandbox) so a missing env alias can't silently block the right channel.
    """
    allowed: set[int] = set()
    for v in explicit or []:
        norm = _normalize_channel_id(v)
        if norm:
            allowed.add(norm)
    if allowed:
        for extra in (ch_feeding_team, ch_sandbox):
            norm = _normalize_channel_id(extra)
            if norm:
                allowed.add(norm)
    return allowed

def _weekday_token(date_iso: str) -> Optional[str]:
    try:
        dt = datetime.fromisoformat(str(date_iso))
    except Exception:
        try:
            dt = datetime.fromisoformat(str(date_iso).replace('Z', '+00:00'))
        except Exception:
            return None
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date().weekday()]


def _authorized_requesters_for_sub(station: str, dates: Iterable[str], files: List[Tuple[str, List[dict]]]) -> set[int]:
    normalized_dates = _normalize_dates(dates) or [_today_iso()]
    station_key = _canonical_station(station) or station
    allowed: set[int] = set()
    for iso in normalized_dates:
        weekday = _weekday_token(iso)
        if not weekday:
            continue
        try:
            tgt = datetime.fromisoformat(iso).date()
        except Exception:
            tgt = None
        sched = _read_schedule_for_weekday(weekday, tgt)
        allowed.update(sched.get(station_key, []))

    for _, rows in files:
        for record in rows:
            if record.get("status") != "accepted":
                continue
            record_station = _canonical_station(record.get("station")) or record.get("station")
            if record_station != station_key:
                continue
            record_dates = _normalize_dates(record.get("dates") or [])
            if set(record_dates) & set(normalized_dates):
                assignee = record.get("assignee")
                if isinstance(assignee, int):
                    allowed.add(assignee)
    return allowed


def _month_key_for_dates(dates: Iterable[str]) -> Optional[str]:
    for iso in _normalize_dates(dates):
        key = _sub_month_key_from_date(iso)
        if key:
            return key
    return None


def _derive_month_key(record: dict, default: Optional[str] = None) -> str:
    key = _month_key_for_dates(record.get("dates") or [])
    if key:
        return key
    for field in ("created_at", "updated_at", "ts"):
        val = record.get(field)
        if not val:
            continue
        try:
            dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
            return _sub_month_key_from_datetime(dt)
        except Exception:
            continue
    if default:
        return default
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    return _sub_month_key_from_datetime(now)


def _load_sub_files(month_keys: Optional[List[str]] = None, include_legacy: bool = True) -> List[Tuple[str, List[dict]]]:
    files: List[Tuple[str, List[dict]]] = []
    seen: set[str] = set()
    keys = month_keys or _recent_month_keys()
    for key in keys:
        try:
            path = _sub_log_path_from_key(key)
        except ValueError:
            continue
        rows = _read_sub_file(path, key)
        files.append((path, rows))
        seen.add(str(path))
    if include_legacy and os.path.exists(SUBS_LEGACY_FILE):
        rows = _read_sub_file(SUBS_LEGACY_FILE, None)
        files.append((SUBS_LEGACY_FILE, rows))
        seen.add(str(SUBS_LEGACY_FILE))
    if month_keys is None:
        for extra in _all_sub_month_keys():
            path = _sub_log_path_from_key(extra)
            if str(path) in seen:
                continue
            rows = _read_sub_file(path, extra)
            files.append((path, rows))
            seen.add(str(path))
    return files

#------------- helpers: schedule/users ----------
def _resolve_user_ids(names: List[str]) -> List[int]:
    """Resolve a list of display names to Discord user IDs via settings.user_id_map.
    Accepts either names or numeric strings.
    """
    cfg_map = {}
    try:
        cfg_map = getattr(settings, "user_id_map", {}) or {}
    except Exception:
        cfg_map = {}
    #normalize keys to simple form
    norm_map: Dict[str, int] = {}
    for k, v in cfg_map.items():
        try:
            uid = int(v)
        except (TypeError, ValueError):
            continue
        key = str(k).strip()
        if not key:
            continue
        variants = {
            key,
            key.lower(),
            key.strip("@"),
            key.strip("@").lower(),
        }
        for var in variants:
            if var:
                norm_map[var] = uid

    ids: List[int] = []
    for n in names:
        if n is None:
            continue
        n1 = str(n).strip()
        if not n1 or n1.lower() == "none":
            continue
        uid = None
        for cand in (n1, n1.lower(), n1.strip("@"), n1.strip("@").lower()):
            if cand in norm_map:
                uid = norm_map[cand]
                break
        if uid is not None:
            ids.append(uid)
            continue
        try:
            ids.append(int(n1))
        except Exception:
            continue
    return ids


def _format_user(bot: Optional[discord.Client], uid: int | str, mention: bool) -> str:
    if mention:
        return f"<@{uid}>"
    name = None
    if bot:
        user = bot.get_user(uid)
        if user:
            name = getattr(user, "global_name", None) or getattr(user, "display_name", None) or getattr(user, "name", None)
    if not name:
        lookup = {}
        try:
            lookup = getattr(settings, "user_id_map", {}) or {}
        except Exception:
            lookup = {}
        for disp, mapped in lookup.items():
            try:
                if int(mapped) == int(uid):
                    name = disp
                    break
            except Exception:
                continue
    if not name:
        name = str(uid)
    return str(name)


def _coerce_uid(val) -> Optional[int | str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return s  #allow non-numeric IDs


def _read_schedule_ndjson(path: Path) -> List[dict]:
    versions: List[dict] = []
    if not path.exists():
        return versions
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
                versions.append({
                    "effective_from": obj.get("effective_from"),
                    "schedule": obj.get("schedule") or {},
                    "meta": obj.get("meta") or {}
                })
    except Exception:
        return versions
    return versions


def _load_schedule_versions() -> List[dict]:
    versions = _read_schedule_ndjson(UI_SCHEDULE_PATH)
    if versions:
        return versions
    if UI_SCHEDULE_PATH_LEGACY.exists():
        try:
            data = json.loads(UI_SCHEDULE_PATH_LEGACY.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "versions" in data:
                versions = data.get("versions") or []
            elif isinstance(data, dict) and "schedule" in data:
                versions = [{"effective_from": _DEFAULT_SCHED_EFFECTIVE, "schedule": data.get("schedule") or {}, "meta": data.get("meta") or {}}]
            elif isinstance(data, list):
                versions = data
            if versions:
                _save_schedule_versions(versions)
            return versions
        except Exception:
            return []
    return []


def _save_schedule_versions(versions: List[dict]) -> None:
    meta = {"updated_at": int(time.time())}
    UI_SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = UI_SCHEDULE_PATH.with_name(UI_SCHEDULE_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for v in versions:
            f.write(json.dumps(v, separators=(",", ":")) + "\n")
        f.write(json.dumps({"meta": meta}, separators=(",", ":")) + "\n")
    tmp.replace(UI_SCHEDULE_PATH)


def _resolve_schedule_for_date(target_date: Optional[date]) -> Dict[str, Any]:
    versions = _load_schedule_versions()
    if not target_date:
        target_date = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    if not versions:
        return {"schedule": {}, "effective_from": _DEFAULT_SCHED_EFFECTIVE}
    best = None
    for v in versions:
        try:
            eff = datetime.fromisoformat(str(v.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)).date()
        except Exception:
            continue
        if eff <= target_date and (best is None or datetime.fromisoformat(str(best.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)).date() < eff):
            best = v
    if not best:
        best = sorted(versions, key=lambda x: x.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)[0]
    sched = best.get("schedule") or {}
    if not isinstance(sched, dict):
        sched = {}
    return {"schedule": sched, "effective_from": best.get("effective_from")}


def _read_schedule_for_weekday(weekday_name: str, target_date: Optional[date] = None) -> Dict[str, List[int | str]]:
    """Read schedule in station->7-day format for the version effective on target_date."""
    resolved = _resolve_schedule_for_date(target_date)
    cfg: Dict[str, List[str]] = resolved.get("schedule", {}) or {}
    wk_names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
    idx = 0
    low = (weekday_name or "").lower()
    for i, w in enumerate(wk_names):
        if low.startswith(w.lower()):
            idx = i
            break
    out: Dict[str, List[int | str]] = {}
    for station, seq in cfg.items():
        station_disp = _canonical_station(station) or str(station).strip()
        if not station_disp:
            continue
        if not isinstance(seq, list) or not seq:
            out.setdefault(station_disp, [])
            continue
        if len(seq) != 7:
            log_action("schedule_warn", f"station={station}", f"len={len(seq)} != 7; cycling")
        raw_val = seq[idx % len(seq)]
        ids = []
        uid = _coerce_uid(raw_val)
        if uid is not None:
            ids = [uid]
        existing = out.get(station_disp, [])
        merged = list(dict.fromkeys(existing + ids)) if ids else existing
        out[station_disp] = merged
    return out


def _canonical_station(station: Optional[str]) -> Optional[str]:
    text = (station or "").strip()
    if not text:
        return None
    try:
        resolved = resolve_station_or_cat(text, want="station")
    except Exception:
        resolved = None
    if resolved:
        return resolved
    return text

#------------- Google Sheets glue (safe stubs) ---
def _get_feeding_checklist_sheet_id() -> Optional[str]:
    #We store the checklist in the Vision sheet under tab "FeedingStationChecklist"
    return getattr(settings, "sheet_vision_id", None) or getattr(settings, "aux_spreadsheet_id", None)

_FEED_WS_CACHE: Tuple[Any, float] | None = None
_FEED_WS_TTL_SEC = 55.0  #refresh roughly every minute

def _open_feeding_ws():
    """Open the FeedingStationChecklist worksheet with short TTL caching and 429 backoff."""
    global _FEED_WS_CACHE
    now = time.monotonic()
    if _FEED_WS_CACHE:
        ws, ts = _FEED_WS_CACHE
        if now - ts < _FEED_WS_TTL_SEC:
            return ws

    sid = _get_feeding_checklist_sheet_id()
    if not sid:
        log_action("feeding_sheet", "missing_sheet_id", "")
        return None
    sid_str = str(sid)
    last_err = None
    for attempt in range(3):
        try:
            gc = sheets_client()
            sh = gc.open_by_key(sid_str)
            ws = sh.worksheet("FeedingStationChecklist")
            _FEED_WS_CACHE = (ws, now)
            return ws
        except APIError as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429 and attempt < 2:
                delay = 1.5 * (attempt + 1)
                log_action("feeding_sheet", "open_retry_429", f"sleep={delay}")
                time.sleep(delay)
                continue
            break
        except Exception as e:  #pragma: no cover - defensive
            last_err = e
            break
    log_action("feeding_sheet", "open_error", str(last_err))
    return None

def _station_header_map(ws) -> Dict[str, int]:
    """Return {display_name: col_index_1based} from header row (Row 1)."""
    try:
        header = ws.row_values(1)
    except Exception as e:
        log_action("feeding_sheet", "header_error", str(e))
        return {}
    out: Dict[str, int] = {}
    for i, name in enumerate(header, start=1):
        nm = str(name or "").strip()
        if nm:
            out[nm] = i
    return out


#------------- Local feeding checklist store -------------
def _load_feeding_checklist_data() -> dict:
    data = _load_json(str(FEEDING_CHECKLIST_PATH), {"stations": [], "days": {}, "meta": {}})
    data["stations"] = data.get("stations") or []
    data["days"] = data.get("days") or {}
    data["meta"] = data.get("meta") or {}
    return data


def _station_universe(existing: Optional[dict] = None) -> List[str]:
    data = existing or _load_feeding_checklist_data()
    names: set[str] = set()
    for st in station_names():
        canon = _canonical_station(st) or st
        if canon:
            names.add(canon)
    for st in data.get("stations", []):
        canon = _canonical_station(st) or st
        if canon:
            names.add(canon)
    versions = _load_schedule_versions()
    sched = {}
    if versions:
        latest = sorted(versions, key=lambda v: v.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)[-1]
        sched = latest.get("schedule", {}) or {}
    for st in sched.keys():
        canon = _canonical_station(st) or st
        if canon:
            names.add(canon)
    return sorted(names)


def get_feeding_snapshot(date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    """Return a slice of the checklist for the requested date range."""
    try:
        d_from = datetime.fromisoformat(date_from).date() if date_from else None
    except Exception:
        d_from = None
    try:
        d_to = datetime.fromisoformat(date_to).date() if date_to else None
    except Exception:
        d_to = None

    data = _load_feeding_checklist_data()
    stations = _station_universe(data)
    days: Dict[str, Dict[str, bool]] = {}
    for iso, day in data.get("days", {}).items():
        try:
            d = datetime.fromisoformat(iso).date()
        except Exception:
            continue
        if d_from and d < d_from:
            continue
        if d_to and d > d_to:
            continue
        # Normalize day map to known stations
        days[iso] = {st: bool(day.get(st, False)) for st in stations}

    return {
        "stations": stations,
        "days": days,
        "meta": {"updated_at": data.get("meta", {}).get("updated_at")}
    }


async def set_feeding_day_status(date_iso: str, status: Dict[str, Any]) -> dict:
    """Persist the given station status map for one date."""
    try:
        clean_date = datetime.fromisoformat(date_iso).date().isoformat()
    except Exception:
        raise ValueError("Invalid date format")

    async with _FEEDING_CHECKLIST_LOCK:
        data = _load_feeding_checklist_data()
        stations = set(_station_universe(data))
        day: Dict[str, bool] = data.get("days", {}).get(clean_date, {})
        for st, val in status.items():
            canon = _canonical_station(st) or st
            if not canon:
                continue
            stations.add(canon)
            day[canon] = bool(val)
        data["stations"] = sorted(stations)
        data["days"][clean_date] = day
        meta = data.get("meta", {}) or {}
        meta["updated_at"] = _now_iso()
        data["meta"] = meta
        _save_json_atomic(FEEDING_CHECKLIST_PATH, data)
        return {clean_date: {st: bool(day.get(st, False)) for st in data["stations"]}}

def _parse_date_str(s: str) -> Optional[str]:
    """Parse common date formats to ISO YYYY-MM-DD."""
    try:
        #YYYY-MM-DD
        if s and len(s) >= 8 and s[4] == '-' and s[7] == '-':
            return str(datetime.fromisoformat(s).date())
    except Exception:
        pass
    #M/D/YYYY or MM/DD/YYYY
    try:
        parts = [p for p in str(s).replace(" ", "").split("/") if p]
        if len(parts) == 3 and len(parts[2]) == 4:
            m = int(parts[0]); d = int(parts[1]); y = int(parts[2])
            return date(y, m, d).isoformat()
    except Exception:
        pass
    return None

def _find_date_row(ws, date_iso: str) -> Optional[int]:
    """Find row index (1-based) where Column A equals date_iso (ISO)."""
    try:
        col = ws.col_values(1)  #date column
    except Exception as e:
        log_action("feeding_sheet", "date_col_error", str(e))
        return None
    for idx, val in enumerate(col[1:], start=2):  #skip header cell A1
        if _parse_date_str(val or "") == date_iso:
            return idx
    return None


def _ensure_date_row(ws, date_iso: str, header: Optional[Dict[str, int]] = None) -> Optional[int]:
    header = header or _station_header_map(ws)
    row = _find_date_row(ws, date_iso)
    if row:
        return row
    max_col = max(header.values()) if header else 1
    values: List[Any] = [None] * max(1, max_col)
    values[0] = date_iso
    for idx in range(1, len(values)):
        values[idx] = False
    try:
        ws.append_row(values, value_input_option="USER_ENTERED")
        log_action("feeding_sheet", "add_row", date_iso)
    except Exception as e:
        log_action("feeding_sheet", "add_row_error", str(e))
        return None
    return _find_date_row(ws, date_iso)

def _sheet_station_names() -> List[str]:
    """Return canonical station names from the local checklist store or schedule."""
    return _station_universe()

async def _mark_checkbox_in_sheet(station: str, date_iso: str) -> bool:
    """Mark a station/date as fed in the local checklist store."""
    try:
        await set_feeding_day_status(date_iso, {station: True})
        log_action("sheet_mark", f"station={station} date={date_iso}", "ok")
        return True
    except Exception as e:
        log_action("sheet_mark_error", f"station={station} date={date_iso}", str(e))
        return False

async def _list_unfed_stations_today() -> List[str]:
    """Return station display names that are NOT checked for today's date."""
    try:
        today_iso = _today_iso()
        snap = get_feeding_snapshot(today_iso, today_iso)
        stations = snap.get("stations") or []
        day = (snap.get("days") or {}).get(today_iso, {}) or {}
        return [st for st in stations if not bool(day.get(st, False))]
    except Exception as e:
        log_action("unfed_list_error", "read", str(e))
        return []

async def handle_feeding_inquiry(intent, ctx: Dict[str, Any]) -> None:
    """Respond with today's feeding completions and outstanding stations."""
    ch = ctx["channel"]
    #Get today’s stations from the configured schedule (fallback to keys union if needed)
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][today.weekday()]
    today_sched = _read_schedule_for_weekday(weekday, today)  #{station: [user_ids]}
    stations = sorted(today_sched.keys())

    unfed = await _list_unfed_stations_today()  #TODO: wire to Sheets
    #If we don’t know all stations from Sheets yet, assume schedule defines the universe
    if not stations:
        stations = sorted(set(_sheet_station_names() or unfed))  #fallback to sheet header or unfed list
    fed = [s for s in stations if s not in set(unfed)]

    lines = []
    lines.append("**Feeding status**")
    lines.append(f"**Fed:** {', '.join(fed) if fed else 'none'}")
    lines.append(f"**Unfed:** {', '.join(unfed) if unfed else 'none'}")
    await safe_send(ch, "\n".join(lines))


#------------- public handler entry points -------
async def handle_feed_update_event(event, ctx: Dict[str, Any]) -> None:
    """
    Event carries: station, dates[], has_image, attachment_ids.
    We mark all given dates as fed in the Sheet (stubbed) and log.
    """
    ch: discord.abc.MessageableChannel = ctx["channel"]
    stations_raw = event.stations if getattr(event, "stations", None) else [event.station]
    stations: List[str] = []
    for s in stations_raw or []:
        canon = _canonical_station(s) or (s or "")
        if canon:
            stations.append(canon)
    if not stations:
        stations = ["Unknown"]
    dates = _normalize_dates(event.dates or [])
    if not dates:
        dates = [_today_iso()]

    #Channel gating: only accept in allowed feeding channels if configured
    explicit_allowed: List[int] = getattr(settings, "allowed_feeding_channel_ids", []) or getattr(settings, "allowed_feeding_channels", [])
    allowed = _resolve_allowed_feeding_channels(
        explicit_allowed,
        getattr(settings, "ch_feeding_team", None),
        getattr(settings, "ch_sandbox", None),
    )
    if allowed:
        ch_id = _normalize_channel_id(getattr(ch, "id", None))
        if ch_id is None or ch_id not in allowed:
            allowed_str = ",".join(str(a) for a in sorted(allowed))
            log_action("feed_update_ignored", f"station={','.join(stations)}", f"channel_blocked:{getattr(ch, 'id', None)}; allowed={allowed_str}")
            return

    ok_any = False
    for st in stations:
        ok_all_dates = True
        for d in dates:
            ok = await _mark_checkbox_in_sheet(st, d)
            ok_all_dates = ok_all_dates and ok
        ok_any = ok_any or ok_all_dates
        status = "ok" if ok_all_dates else "partial"
        log_action("feed_update", f"station={st}; dates={','.join(dates)}", status)
    if not ok_any:
        log_action("feed_update", f"station={','.join(stations)}; dates={','.join(dates)}", "none_marked")

async def handle_sub_request_event(event, ctx: Dict[str, Any]) -> None:
    """
    Log a sub request locally and post a small accept/decline UI.
    Assumes event.station may be None and event.dates may be None.
    """
    station = _canonical_station(event.station) or (event.station or "")
    dates = _normalize_dates(event.dates or [])
    if not dates:
        dates = [_today_iso()]
    now_iso = _now_iso()
    snippet = _message_preview(event.text)
    trigger_phrase = event.trigger_phrase or ""

    async with _SUBS_LOCK:
        recent_files = _load_sub_files(_recent_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        all_files = _load_sub_files(_all_sub_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))

        allowed_requesters = _authorized_requesters_for_sub(station, dates, all_files)
        admin_ids = set(getattr(settings, "admin_ids", []) or [])
        if allowed_requesters and event.user_id not in allowed_requesters and event.user_id not in admin_ids:
            allowed_str = ",".join(str(uid) for uid in sorted(allowed_requesters))
            log_action(
                "sub_request_denied",
                f"station={station}; dates={','.join(dates)}",
                f"user={event.user_id}; allowed={allowed_str}; text={snippet}",
            )
            return

        if not allowed_requesters:
            log_action(
                "sub_request_warn",
                f"station={station}; dates={','.join(dates)}",
                f"user={event.user_id}; reason=no_schedule; text={snippet}",
            )

        if _update_existing_sub_request(recent_files, station, dates, event, now_iso):
            return

        if _update_existing_sub_request(all_files, station, dates, event, now_iso):
            return

        month_key = _month_key_for_dates(dates) or _sub_month_key_from_datetime(datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now())
        rec = {
            "kind": "sub_request",
            "id": f"sub-{event.message_id}",
            "station": station,
            "dates": dates,
            "requester": event.user_id,
            "assignee": None,
            "status": "requested",
            "channel_id": event.channel_id,
            "message_id": event.message_id,
            "created_at": now_iso,
            "log_month": month_key,
            "message_preview": snippet,
            "trigger_phrase": trigger_phrase,
        }
        _append_sub_record(rec, month_key)
        log_action(
            "sub_request",
            f"station={station}; dates={','.join(dates)}",
            f"status=logged; trigger={trigger_phrase}; text={snippet}",
        )

    #No UI; subs are fully silent by design

async def handle_sub_accept_event(event, ctx: Dict[str, Any]) -> None:
    """
    Someone said 'sure/I can cover'. Assign them to the most recent open request in this channel.
    """
    desired_dates = _normalize_dates(event.dates or [])
    station_canon = _canonical_station(event.station) if event.station else None
    preview = _message_preview(event.text)
    accepted_id = await _accept_latest_open_sub_in_channel(
        event.channel_id,
        event.user_id,
        station=station_canon,
        dates=desired_dates,
        message_preview=preview,
        trigger_phrase=event.trigger_phrase or "",
    )
    if accepted_id:
        accept_record = {
            "kind": "sub_accept",
            "sub_id": accepted_id,
            "assignee": event.user_id,
            "channel_id": event.channel_id,
            "message_id": event.message_id,
            "ts": _now_iso(),
            "message_preview": preview,
            "trigger_phrase": event.trigger_phrase or "",
        }
        async with _SUBS_LOCK:
            month_key = _derive_month_key(accept_record)
            accept_record["log_month"] = month_key
            _append_sub_record(accept_record, month_key)
        log_action(
            "sub_accept",
            f"user={event.user_id}; sub_id={accepted_id}",
            f"status=ok; text={preview}",
        )
    else:
        log_action(
            "sub_accept",
            f"user={event.user_id}",
            f"no_open_request; text={preview}",
        )

#------------- persistence for subs ------------
async def _accept_latest_open_sub_in_channel(
    channel_id: int,
    assignee_id: int,
    *,
    station: Optional[str] = None,
    dates: Optional[List[str]] = None,
    message_preview: str = "",
    trigger_phrase: str = "",
) -> Optional[str]:
    """Helper that marks the newest open sub request as filled."""
    async with _SUBS_LOCK:
        files = _load_sub_files(_recent_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        accepted = _accept_sub_in_files(
            files,
            channel_id,
            assignee_id,
            station=station,
            dates=dates,
            message_preview=message_preview,
            trigger_phrase=trigger_phrase,
        )
        if accepted:
            return accepted
        #Fallback to full history if not found in recent months
        files = _load_sub_files(_all_sub_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        return _accept_sub_in_files(
            files,
            channel_id,
            assignee_id,
            station=station,
            dates=dates,
            message_preview=message_preview,
            trigger_phrase=trigger_phrase,
        )


def _accept_sub_in_files(
    files: List[Tuple[str, List[dict]]],
    channel_id: int,
    assignee_id: int,
    *,
    station: Optional[str] = None,
    dates: Optional[List[str]] = None,
    message_preview: str = "",
    trigger_phrase: str = "",
) -> Optional[str]:
    today_iso = _today_iso()
    now_iso = _now_iso()
    desired_station = _canonical_station(station) if station else None
    desired_dates = _normalize_dates(dates or [])
    for path, rows in files:
        for idx in range(len(rows) - 1, -1, -1):
            record = rows[idx]
            if record.get("channel_id") != channel_id or record.get("status") != "requested":
                continue
            record_station = _canonical_station(record.get("station")) or record.get("station")
            if desired_station and record_station != desired_station:
                continue
            record_dates = _normalize_dates(record.get("dates") or [])
            if not record_dates:
                record_dates = [today_iso]
            if desired_dates and not (set(record_dates) & set(desired_dates)):
                continue
            record["status"] = "accepted"
            record["assignee"] = assignee_id
            record["dates"] = record_dates
            record["station"] = record_station
            record["updated_at"] = now_iso
            record["log_month"] = record.get("log_month") or _derive_month_key(record)
            if message_preview:
                record["accept_message_preview"] = message_preview
            if trigger_phrase:
                record["accept_trigger_phrase"] = trigger_phrase
            rows[idx] = record
            _write_sub_file(path, rows)
            return str(record.get("id")) if record.get("id") else None
    return None


def _update_existing_sub_request(
    files: List[Tuple[str, List[dict]]],
    station: str,
    dates: List[str],
    event,
    now_iso: str,
) -> bool:
    for path, rows in files:
        for idx in range(len(rows) - 1, -1, -1):
            record = rows[idx]
            if record.get("status") != "requested":
                continue
            record_station = _canonical_station(record.get("station")) or record.get("station")
            if record_station != station:
                continue
            existing_dates = _normalize_dates(record.get("dates") or [])
            if not set(existing_dates) & set(dates):
                continue
            merged_dates = sorted(set(existing_dates) | set(dates))
            preview = _message_preview(event.text)
            record.update({
                "station": station,
                "dates": merged_dates,
                "requester": event.user_id,
                "channel_id": event.channel_id,
                "message_id": event.message_id,
                "updated_at": now_iso,
                "message_preview": preview,
                "trigger_phrase": event.trigger_phrase or record.get("trigger_phrase"),
            })
            record["log_month"] = record.get("log_month") or _derive_month_key(record)
            rows[idx] = record
            _write_sub_file(path, rows)
            log_action(
                "sub_request",
                f"station={station}; dates={','.join(merged_dates)}",
                f"status=updated; trigger={event.trigger_phrase or ''}; text={preview}",
            )
            return True
    return False

#------------- scheduler: 6:30 am ping ----------

_MORNING_SCHEDULER_LOCK = asyncio.Lock()
_MORNING_SCHEDULER_STARTED = False


async def build_morning_message(bot: discord.Client) -> tuple[str, discord.ui.View | None]:
    """Builds the 7:45 AM 'Good Morning' message with the day's feeding schedule."""
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    today_iso = today.isoformat()
    # This looks odd, but is how the 8pm scheduler gets the weekday name for the schedule lookup.
    # Monday (weekday() == 0) becomes "Mon", which _read_schedule_for_weekday finds at index 1.
    # Sunday (weekday() == 6) becomes "Sun", which is at index 0. It's quirky but consistent.
    weekday_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][today.weekday()]
    
    sched = _read_schedule_for_weekday(weekday_name, today)
    todays_stations = sorted([s for s in sched.keys() if sched.get(s)])

    async with _SUBS_LOCK:
        files = _load_sub_files(_all_sub_month_keys(), include_legacy=True)
        subs = [item for sublist in files for item in sublist[1]]

    # Map accepted request IDs to assignees for today
    accepted_req_map = {
        r.get("parent_id"): r.get("assignee") 
        for r in subs 
        if r.get("status") == "accepted" 
        and today_iso in _normalize_dates(r.get("dates") or [])
        and r.get("parent_id")
    }

    lines = ["Good Morning!", "Todays currently scheduled feeders are:"]
    open_request_exists = False

    for station in todays_stations:
        roster_parts = []
        original_feeders = sched.get(station, [])
        
        for feeder_id in original_feeders:
            feeder_request_id = None
            for req in subs:
                if (req.get("status") == "requested" 
                    and str(req.get("requester")) == str(feeder_id) 
                    and _canonical_station(req.get("station")) == station 
                    and today_iso in _normalize_dates(req.get("dates") or [])):
                    feeder_request_id = req.get("id")
                    break
            
            if feeder_request_id:
                sub_assignee_id = accepted_req_map.get(feeder_request_id)
                if sub_assignee_id:
                    roster_parts.append(_format_user(bot, sub_assignee_id, False))
                else:
                    original_feeder_name = _format_user(bot, feeder_id, False)
                    roster_parts.append(f"**NEEDS SUB** (for {original_feeder_name})")
                    open_request_exists = True
            else:
                roster_parts.append(_format_user(bot, feeder_id, False))
        
        lines.append(f"• **{station}**: {', '.join(roster_parts)}")

    if open_request_exists:
        lines.append("\nA station is still looking for a substitute feeder.")

    view = None
    if open_request_exists:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Sub Requests", url="https://ui.catsofuta.org/#sub"))
    
    return "\n".join(lines), view

async def start_morning_scheduler(bot: discord.Client) -> None:
    """Kick off the daily 7:40am morning message."""
    global _MORNING_SCHEDULER_STARTED
    async with _MORNING_SCHEDULER_LOCK:
        if _MORNING_SCHEDULER_STARTED:
            return

        async def _runner():
            while True:
                try:
                    await _sleep_until_local_time(7, 40)
                    channel_id = getattr(settings, "ch_feeding_team", None)
                    if not channel_id:
                        log_action("morning_scheduler", "channel=None", "skipped")
                        continue
                    
                    ch = bot.get_channel(int(channel_id))
                    if not isinstance(ch, discord.abc.Messageable):
                        log_action("morning_scheduler", f"channel={channel_id}", "not_messageable")
                        continue

                    message_content, view = await build_morning_message(bot)
                    await ch.send(message_content, view=view)
                    log_action("morning_scheduler", "sent", f"channel={channel_id}")

                except Exception as e:
                    log_action("morning_scheduler_error", "loop", str(e))
                    await asyncio.sleep(60)

        asyncio.create_task(_runner())
        _MORNING_SCHEDULER_STARTED = True
        log_action("morning_scheduler", "started", "ok")



#------------- scheduler: 8:00 pm ping ----------

_FEEDING_SCHEDULER_LOCK = asyncio.Lock()
_FEEDING_SCHEDULER_STARTED = False
_FEEDING_8PM_LOCK = asyncio.Lock()
_LAST_FEEDING_ALERT_KEY: Optional[str] = None
_LAST_FEEDING_ALERT_TS: Optional[datetime] = None

async def start_feeding_scheduler(bot: discord.Client) -> None:
    """Kick off the nightly reminder loop that pings unfed stations."""
    global _FEEDING_SCHEDULER_STARTED
    async with _FEEDING_SCHEDULER_LOCK:
        if _FEEDING_SCHEDULER_STARTED:
            log_action("feeding_scheduler", "already_started", "skipped")
            return

        async def _runner():
            while True:
                try:
                    #sleep until next 20:00 America/Chicago
                    await _sleep_until_local_time(20, 0)
                    await _run_8pm_check(bot)
                except Exception as e:
                    log_action("feeding_scheduler_error", "loop", str(e))
                    await asyncio.sleep(10)

        asyncio.create_task(_runner())
        _FEEDING_SCHEDULER_STARTED = True
        log_action("feeding_scheduler", "started", "ok")


async def _sleep_until_local_time(hour: int, minute: int):
    """Suspend until the next scheduled run in the configured timezone."""
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

async def _run_8pm_check(bot: discord.Client, *, force: bool = False) -> None:
    """Build and send the 8PM summary of remaining feed duties."""
    global _LAST_FEEDING_ALERT_KEY, _LAST_FEEDING_ALERT_TS
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    today_key = now.date().isoformat()
    async with _FEEDING_8PM_LOCK:
        if not force and _LAST_FEEDING_ALERT_KEY == today_key:
            log_action("feeding_8pm", f"date={today_key}", "duplicate_skip")
            return
        _LAST_FEEDING_ALERT_KEY = today_key
        _LAST_FEEDING_ALERT_TS = now

    sent_or_logged = False

    #Use feeding team channel for alerts
    channel_id = getattr(settings, "ch_feeding_team", None)
    if not channel_id:
        log_action("feeding_8pm", "channel=None", "skipped (no alert channel configured)")
        sent_or_logged = False
        async with _FEEDING_8PM_LOCK:
            if _LAST_FEEDING_ALERT_KEY == today_key:
                _LAST_FEEDING_ALERT_KEY = None
                _LAST_FEEDING_ALERT_TS = None
        return

    ch = bot.get_channel(int(channel_id))
    if not ch:
        log_action("feeding_8pm", f"channel={channel_id}", "not_found")
        async with _FEEDING_8PM_LOCK:
            if _LAST_FEEDING_ALERT_KEY == today_key:
                _LAST_FEEDING_ALERT_KEY = None
                _LAST_FEEDING_ALERT_TS = None
        return

    unfed = await _list_unfed_stations_today()
    from discord.abc import Messageable

    if not unfed:
        msg = "All stations have been fed! Yippee!!!"
        try:
            if isinstance(ch, Messageable):
                await safe_send(ch, msg)
                log_action("feeding_8pm", "unfed=0", "celebrated")
                sent_or_logged = True
            else:
                log_action("feeding_8pm", f"channel={channel_id}; type={type(ch).__name__}", "not_messageable")
                sent_or_logged = True
        except Exception as e:
            log_action("feeding_8pm_error", "unfed=0", str(e))
        if not sent_or_logged:
            async with _FEEDING_8PM_LOCK:
                if _LAST_FEEDING_ALERT_KEY == today_key:
                    _LAST_FEEDING_ALERT_KEY = None
                    _LAST_FEEDING_ALERT_TS = None
        return

    #choose who to ping: subs first, else default schedule
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][today.weekday()]
    sched = _read_schedule_for_weekday(weekday, today)

    #Build a message that pings the right people
    lines = await build_8pm_lines(bot, unfed=unfed, sched=sched, mention=True)

    try:
        if isinstance(ch, Messageable):
            await safe_send(ch, lines)  #silent mode respected here
            log_action("feeding_8pm", f"unfed={len(unfed)}", "sent")
            sent_or_logged = True
        else:
            log_action("feeding_8pm", f"channel={channel_id}; type={type(ch).__name__}", "not_messageable")
            sent_or_logged = True
    except Exception as e:
        log_action("feeding_8pm_error", f"unfed={len(unfed)}", str(e))
        sent_or_logged = False

    if not sent_or_logged:
        async with _FEEDING_8PM_LOCK:
            if _LAST_FEEDING_ALERT_KEY == today_key:
                _LAST_FEEDING_ALERT_KEY = None
                _LAST_FEEDING_ALERT_TS = None

async def build_8pm_lines(
    bot: Optional[discord.Client],
    *,
    unfed: Optional[List[str]] = None,
    sched: Optional[Dict[str, List[int | str]]] = None,
    mention: bool = True,
    include_fed: bool = False,
) -> str:
    """Build the text for the 8pm message. mention=True uses <@id> tags; else shows @username/ID.
    If unfed/sched not provided, computes them.
    """
    if unfed is None:
        unfed = await _list_unfed_stations_today()
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    if sched is None:
        weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][today.weekday()]
        sched = _read_schedule_for_weekday(weekday, today)
    async with _SUBS_LOCK:
        files = _load_sub_files(_recent_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        subs: List[dict] = []
        for _, rows in files:
            subs.extend(rows)
    today_iso = today.isoformat()

    sched = sched or {}

    def _assignees_for(station: str) -> List[int | str]:
        key = _canonical_station(station) or station
        scheduled_ids = sched.get(key, [])
        if not scheduled_ids:
            # Check for subs for unassigned stations
            for r in reversed(subs):
                if r.get("status") != "accepted": continue
                rec_station = _canonical_station(r.get("station")) or r.get("station")
                if rec_station != key: continue
                if today_iso not in _normalize_dates(r.get("dates") or []): continue
                
                assignee_id = r.get("assignee")
                if assignee_id:
                    return [assignee_id] # Return the sub, even if no one was scheduled
            return []

        final_assignees = list(scheduled_ids)

        request_map: Dict[str, int] = {
            r.get("id"): r.get("requester")
            for r in subs
            if r.get("status") == "requested" and r.get("id") and r.get("requester")
        }

        processed_requesters = set()

        for r in reversed(subs):
            if r.get("status") != "accepted":
                continue
                
            rec_station = _canonical_station(r.get("station")) or r.get("station")
            if rec_station != key:
                continue

            if today_iso not in _normalize_dates(r.get("dates") or []):
                continue
            
            parent_id = r.get("parent_id")
            requester_id = request_map.get(parent_id)
            assignee_id = r.get("assignee")
            
            if not requester_id and r.get("id"):
                parent_id = r.get("id")
                requester_id = request_map.get(parent_id)

            if requester_id and assignee_id and requester_id in final_assignees:
                if requester_id in processed_requesters:
                    continue
                
                final_assignees = [assignee_id if u == requester_id else u for u in final_assignees]
                processed_requesters.add(requester_id)

        return list(dict.fromkeys(final_assignees))

    def _format_station_line(station: str) -> str:
        assignees = _assignees_for(station)
        if assignees:
            roster = " ".join(_format_user(bot, uid, mention) for uid in assignees)
        else:
            roster = "unassigned"
        return f"• **{station}** → {roster}"

    if not include_fed:
        lines: List[str] = ["**Currently unfed stations**"]
        if not unfed:
            lines.append("none")
            return "\n".join(lines)
        for st in unfed:
            lines.append(_format_station_line(st))
        return "\n".join(lines)

    stations = list(sched.keys()) if sched else []
    unfed = unfed or []
    unfed_set = set(unfed)
    fed = [st for st in stations if st not in unfed_set]

    sections: List[str] = []

    sections.append("**Fed stations**")
    if fed:
        for st in fed:
            sections.append(_format_station_line(st))
    else:
        sections.append("none")

    sections.append("**Unfed stations**")
    if unfed:
        for st in unfed:
            sections.append(_format_station_line(st))
    else:
        sections.append("• none")

    return "\n".join(sections)


async def build_schedule_for_date(
    bot: Optional[discord.Client],
    target_date: date,
    *,
    mention: bool = False,
) -> str:
    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][target_date.weekday()]
    sched = _read_schedule_for_weekday(weekday, target_date) or {}

    target_dt = datetime.combine(target_date, datetime.min.time())
    target_key = _sub_month_key_from_datetime(target_dt)
    month_keys = list(dict.fromkeys(_recent_month_keys() + ([target_key] if target_key else [])))

    async with _SUBS_LOCK:
        files = _load_sub_files(month_keys, include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        subs: List[dict] = []
        for _, rows in files:
            subs.extend(rows)

    target_iso = target_date.isoformat()
    accepted: Dict[str, int] = {}
    for rec in reversed(subs):
        if rec.get("status") != "accepted":
            continue
        dates = _normalize_dates(rec.get("dates") or [])
        if target_iso not in dates:
            continue
        station = _canonical_station(rec.get("station")) or rec.get("station")
        assignee = rec.get("assignee")
        if station and isinstance(assignee, int) and station not in accepted:
            accepted[station] = assignee

    stations: List[str] = list(sched.keys())
    for st in accepted.keys():
        if st not in stations:
            stations.append(st)

    header = target_date.strftime("%A, %B %d, %Y").replace(" 0", " ")
    lines: List[str] = [f"**Feeding schedule for {header}**"]

    if not stations:
        lines.append("No schedule configured.")
        return "\n".join(lines)

    for station in stations:
        default_ids = sched.get(station, []) or []
        sub_uid = accepted.get(station)
        base_names = [_format_user(bot, uid, mention) for uid in default_ids]
        if sub_uid:
            sub_name = _format_user(bot, sub_uid, mention)
            if default_ids and sub_uid not in default_ids and base_names:
                roster_text = f"{sub_name} (covering {' '.join(base_names)})"
            elif default_ids and sub_uid in default_ids:
                roster_text = f"{sub_name}"
            else:
                roster_text = f"{sub_name} (sub)"
        else:
            if base_names:
                roster_text = " ".join(base_names)
            else:
                roster_text = "unassigned"
        lines.append(f"• **{station}** → {roster_text}")

    return "\n".join(lines)

async def handle_manual_8pm_preview(intent, ctx: Dict[str, Any]) -> None:
    """Admin-only: post a dry-run of the 8pm message to the current channel (no pings)."""
    author = ctx["author"]
    uid = int(getattr(author, 'id', 0))
    if uid not in (getattr(settings, 'admin_ids', []) or []):
        log_action("manual_8pm_denied", f"user={uid}", "not_admin")
        return
    bot = ctx.get("bot")
    msg = await build_8pm_lines(bot, mention=False)
    await safe_send(ctx["channel"], msg)
    log_action("manual_8pm", f"by={uid}", "preview_sent")


async def handle_feeding_today(intent, ctx: Dict[str, Any]) -> None:
    """Post today's unfed schedule using plain display names (no mentions)."""
    bot = ctx.get("bot")
    lines = await build_8pm_lines(bot, mention=False, include_fed=True)
    await safe_send(ctx["channel"], lines)
    author = ctx.get("author")
    uid = int(getattr(author, 'id', 0)) if author else 0
    log_action("feeding_today", f"by={uid}", "sent")


async def handle_feeding_schedule(intent, ctx: Dict[str, Any]) -> None:
    ch = ctx.get("channel")
    bot = ctx.get("bot")
    data = intent.data if intent else {}
    iso = data.get("date") or (data.get("dates") or [None])[0]
    if not iso:
        await safe_send(ch, "I couldn't understand that date.")
        return
    try:
        target_date = datetime.fromisoformat(str(iso)).date()
    except Exception:
        await safe_send(ch, "I couldn't understand that date.")
        return
    lines = await build_schedule_for_date(bot, target_date, mention=False)
    await safe_send(ch, lines)
    author = ctx.get("author")
    uid = int(getattr(author, 'id', 0)) if author else 0
    log_action("feeding_schedule", f"by={uid}", f"date={target_date.isoformat()}")
