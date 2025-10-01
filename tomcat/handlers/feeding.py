"""Feeding log handlers: subs, checklist updates, and reminder workflow."""

# tomcat/feeding.py
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord

from ..config import settings
from ..logger import log_action
from ..services.sheets_client import sheets_client
from ..aliases import resolve_station_or_cat
from ..utils.sender import safe_send

# Optional TZ support
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # type: ignore

CENTRAL_TZ = ZoneInfo("America/Chicago") if ZoneInfo else None

# ------------- subs log -------------
# Monthly JSONL files under logs/subs/<year>/<year-month>.jsonl
SUBS_ROOT = os.path.join("logs", "subs")
os.makedirs(SUBS_ROOT, exist_ok=True)
SUBS_LEGACY_FILE = os.path.join(SUBS_ROOT, "subs.jsonl")
_SUBS_LOCK = asyncio.Lock()

# ------------- simple data types ----------------
@dataclass
class SubRecord:
    id: str
    station: str
    dates: List[str]
    requester: int
    assignee: Optional[int]
    status: str  # "requested" | "accepted" | "declined"
    channel_id: int
    message_id: int
    created_at: str

# ------------- helpers: time/date ---------------
def _today_iso() -> str:
    """Return today's date string using the configured timezone."""
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    return now.date().isoformat()

def _now_iso() -> str:
    """Return an ISO timestamp with timezone awareness if available."""
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    return now.isoformat()

# ------------- helpers: files/json --------------
def _load_json(path: str, default):
    """Read a JSON file, returning default on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

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
    folder = os.path.join(SUBS_ROOT, f"{year}")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{year}-{month:02d}.jsonl")


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
    if os.path.exists(SUBS_ROOT):
        for entry in os.listdir(SUBS_ROOT):
            year_path = os.path.join(SUBS_ROOT, entry)
            if not os.path.isdir(year_path):
                continue
            for fname in os.listdir(year_path):
                if fname.endswith(".jsonl"):
                    keys.add(fname[:-6])
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
        seen.add(path)
    if include_legacy and os.path.exists(SUBS_LEGACY_FILE):
        rows = _read_sub_file(SUBS_LEGACY_FILE, None)
        files.append((SUBS_LEGACY_FILE, rows))
        seen.add(SUBS_LEGACY_FILE)
    if month_keys is None:
        for extra in _all_sub_month_keys():
            path = _sub_log_path_from_key(extra)
            if path in seen:
                continue
            rows = _read_sub_file(path, extra)
            files.append((path, rows))
            seen.add(path)
    return files

# ------------- helpers: schedule/users ----------
def _resolve_user_ids(names: List[str]) -> List[int]:
    """Resolve a list of display names to Discord user IDs via settings.user_id_map.
    Accepts either names or numeric strings.
    """
    cfg_map = getattr(settings, "user_id_map", {}) or {}
    # normalize keys to simple form
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

def _read_schedule_for_weekday(weekday_name: str) -> Dict[str, List[int]]:
    """Read schedule from settings.feeding_schedule in station→7-day format.
    Expected format in config:
      feeding_schedule = {
         "Business": ["Chris","Chris","Chris","Megan","Megan","Megan","Ben"],  # Sun..Sat
         "HOP": [...],
      }
    Returns mapping {station_display: [user_id]} for the specific weekday.
    """
    cfg: Dict[str, List[str]] = getattr(settings, "feeding_schedule", {}) or {}
    wk_names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
    idx = 0
    low = (weekday_name or "").lower()
    for i, w in enumerate(wk_names):
        if low.startswith(w.lower()):
            idx = i
            break
    out: Dict[str, List[int]] = {}
    for station, seq in cfg.items():
        station_disp = _canonical_station(station) or str(station).strip()
        if not station_disp:
            continue
        if not isinstance(seq, list) or not seq:
            out.setdefault(station_disp, [])
            continue
        if len(seq) != 7:
            log_action("schedule_warn", f"station={station}", f"len={len(seq)} != 7; cycling")
        name = seq[idx % len(seq)]
        ids = []
        if name not in (None, "", "None"):
            ids = _resolve_user_ids([name])
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

# ------------- Google Sheets glue (safe stubs) ---
def _get_feeding_checklist_sheet_id() -> Optional[str]:
    # We store the checklist in the Vision sheet under tab "FeedingStationChecklist"
    return getattr(settings, "sheet_vision_id", None) or getattr(settings, "aux_spreadsheet_id", None)

def _open_feeding_ws():
    sid = _get_feeding_checklist_sheet_id()
    if not sid:
        log_action("feeding_sheet", "missing_sheet_id", "")
        return None
    try:
        gc = sheets_client()
        sh = gc.open_by_key(sid)
        return sh.worksheet("FeedingStationChecklist")
    except Exception as e:
        log_action("feeding_sheet", "open_error", str(e))
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

def _parse_date_str(s: str) -> Optional[str]:
    """Parse common date formats to ISO YYYY-MM-DD."""
    try:
        # YYYY-MM-DD
        if s and len(s) >= 8 and s[4] == '-' and s[7] == '-':
            return str(datetime.fromisoformat(s).date())
    except Exception:
        pass
    # M/D/YYYY or MM/DD/YYYY
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
        col = ws.col_values(1)  # date column
    except Exception as e:
        log_action("feeding_sheet", "date_col_error", str(e))
        return None
    for idx, val in enumerate(col[1:], start=2):  # skip header cell A1
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

async def _mark_checkbox_in_sheet(station: str, date_iso: str) -> bool:
    """Mark the (station, date) cell TRUE in the FeedingStationChecklist tab.
    Header row (1) has stations; first column (A) has dates; body is checkboxes.
    """
    ws = _open_feeding_ws()
    if ws is None:
        return False
    try:
        header = _station_header_map(ws)
        disp = _canonical_station(station) or station
        col = header.get(disp)
        if not col:
            for name in header.keys():
                if name.lower() == disp.lower():
                    col = header[name]
                    disp = name
                    break
        if not col:
            log_action("sheet_mark_error", f"station={station} date={date_iso}", "missing_column")
            return False
        row = _ensure_date_row(ws, date_iso, header)
        if not row:
            log_action("sheet_mark_error", f"station={station} date={date_iso}", "row_create_failed")
            return False
        ws.update_cell(row, col, True)
        log_action("sheet_mark", f"station={disp} date={date_iso}", "ok")
        return True
    except Exception as e:
        log_action("sheet_mark_error", f"station={station} date={date_iso}", str(e))
        return False

async def _list_unfed_stations_today() -> List[str]:
    """Return station display names that are NOT checked for today's date.
    Station names come from header row; today row comes from Column A.
    """
    ws = _open_feeding_ws()
    if ws is None:
        return []
    try:
        today_iso = _today_iso()
        header = _station_header_map(ws)
        row = _ensure_date_row(ws, today_iso, header)
        if not row:
            log_action("unfed_list", f"date={today_iso}", "date_row_not_found")
            return []
        # Read entire row values once
        vals = ws.row_values(row)
        unfed: List[str] = []
        for name, col in header.items():
            if col == 1:
                continue  # date column
            v = vals[col-1] if col-1 < len(vals) else ""
            fed = False
            if isinstance(v, bool):
                fed = bool(v)
            else:
                fed = str(v).strip().upper() == "TRUE"
            if not fed:
                disp = _canonical_station(name) or name
                if disp not in unfed:
                    unfed.append(disp)
        return unfed
    except Exception as e:
        log_action("unfed_list_error", "read", str(e))
        return []

async def handle_feeding_inquiry(intent, ctx: Dict[str, Any]) -> None:
    """Respond with today's feeding completions and outstanding stations."""
    ch = ctx["channel"]
    # Get today’s stations from the configured schedule (fallback to keys union if needed)
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][today.weekday()]
    today_sched = _read_schedule_for_weekday(weekday)  # {station: [user_ids]}
    stations = sorted(today_sched.keys())

    unfed = await _list_unfed_stations_today()  # TODO: wire to Sheets
    # If we don’t know all stations from Sheets yet, assume schedule defines the universe
    if not stations:
        stations = sorted(set(unfed))  # minimal fallback
    fed = [s for s in stations if s not in set(unfed)]

    lines = []
    lines.append("**Feeding status**")
    lines.append(f"**Fed:** {', '.join(fed) if fed else 'none'}")
    lines.append(f"**Unfed:** {', '.join(unfed) if unfed else 'none'}")
    await safe_send(ch, "\n".join(lines))


# ------------- public handler entry points -------
async def handle_feed_update_event(event, ctx: Dict[str, Any]) -> None:
    """
    Event carries: station, dates[], has_image, attachment_ids.
    We mark all given dates as fed in the Sheet (stubbed) and log.
    """
    ch: discord.abc.MessageableChannel = ctx["channel"]
    station_raw = event.station or ""
    station = _canonical_station(station_raw) or (station_raw or "Unknown")
    dates = _normalize_dates(event.dates or [])
    if not dates:
        dates = [_today_iso()]

    # Channel gating: only accept in allowed feeding channels if configured
    allowed: List[int] = getattr(settings, "allowed_feeding_channel_ids", []) or getattr(settings, "allowed_feeding_channels", [])
    if isinstance(allowed, list) and len(allowed) > 0:
        ch_id = getattr(ch, "id", None)
        if ch_id not in allowed:
            log_action("feed_update_ignored", f"station={station}", f"channel_blocked:{ch_id}")
            return

    ok_all = True
    for d in dates:
        ok = await _mark_checkbox_in_sheet(station, d)
        ok_all = ok_all and ok

    status = "ok" if ok_all else "partial"
    log_action("feed_update", f"station={station}; dates={','.join(dates)}", status)

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

    async with _SUBS_LOCK:
        files = _load_sub_files(_recent_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        if _update_existing_sub_request(files, station, dates, event, now_iso):
            return

        files = _load_sub_files(_all_sub_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        if _update_existing_sub_request(files, station, dates, event, now_iso):
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
        }
        _append_sub_record(rec, month_key)
        log_action("sub_request", f"station={station}; dates={','.join(dates)}", "logged")

    # No UI; subs are fully silent by design

async def handle_sub_accept_event(event, ctx: Dict[str, Any]) -> None:
    """
    Someone said 'sure/I can cover'. Assign them to the most recent open request in this channel.
    """
    accepted_id = await _accept_latest_open_sub_in_channel(event.channel_id, event.user_id)
    if accepted_id:
        accept_record = {
            "kind": "sub_accept",
            "sub_id": accepted_id,
            "assignee": event.user_id,
            "channel_id": event.channel_id,
            "message_id": event.message_id,
            "ts": _now_iso(),
        }
        async with _SUBS_LOCK:
            month_key = _derive_month_key(accept_record)
            accept_record["log_month"] = month_key
            _append_sub_record(accept_record, month_key)
        log_action("sub_accept", f"user={event.user_id}", "ok")
    else:
        log_action("sub_accept", f"user={event.user_id}", "no_open_request")

# ------------- persistence for subs ------------
async def _accept_latest_open_sub_in_channel(channel_id: int, assignee_id: int) -> Optional[str]:
    """Helper that marks the newest open sub request as filled."""
    async with _SUBS_LOCK:
        files = _load_sub_files(_recent_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        accepted = _accept_sub_in_files(files, channel_id, assignee_id)
        if accepted:
            return accepted
        # Fallback to full history if not found in recent months
        files = _load_sub_files(_all_sub_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        return _accept_sub_in_files(files, channel_id, assignee_id)


def _accept_sub_in_files(files: List[Tuple[str, List[dict]]], channel_id: int, assignee_id: int) -> Optional[str]:
    today_iso = _today_iso()
    now_iso = _now_iso()
    for path, rows in files:
        for idx in range(len(rows) - 1, -1, -1):
            record = rows[idx]
            if record.get("channel_id") != channel_id or record.get("status") != "requested":
                continue
            record["status"] = "accepted"
            record["assignee"] = assignee_id
            record_dates = _normalize_dates(record.get("dates") or [])
            if not record_dates:
                record_dates = [today_iso]
            record["dates"] = record_dates
            record["station"] = _canonical_station(record.get("station")) or record.get("station")
            record["updated_at"] = now_iso
            record["log_month"] = record.get("log_month") or _derive_month_key(record)
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
            record.update({
                "station": station,
                "dates": merged_dates,
                "requester": event.user_id,
                "channel_id": event.channel_id,
                "message_id": event.message_id,
                "updated_at": now_iso,
            })
            record["log_month"] = record.get("log_month") or _derive_month_key(record)
            rows[idx] = record
            _write_sub_file(path, rows)
            log_action("sub_request", f"station={station}; dates={','.join(merged_dates)}", "updated")
            return True
    return False

# ------------- scheduler: 8:00 pm ping ----------
async def start_feeding_scheduler(bot: discord.Client) -> None:
    """Kick off the nightly reminder loop that pings unfed stations."""
    async def _runner():
        while True:
            try:
                # sleep until next 20:00 America/Chicago
                await _sleep_until_local_time(20, 0)
                await _run_8pm_check(bot)
            except Exception as e:
                log_action("feeding_scheduler_error", "loop", str(e))
                await asyncio.sleep(10)

    asyncio.create_task(_runner())

async def _sleep_until_local_time(hour: int, minute: int):
    """Suspend until the next scheduled run in the configured timezone."""
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

async def _run_8pm_check(bot: discord.Client) -> None:
    """Build and send the 8PM summary of remaining feed duties."""
    # compute unfed stations from sheet
    unfed = await _list_unfed_stations_today()
    if not unfed:
        log_action("feeding_8pm", "unfed=0", "nothing_to_ping")
        return

    # choose who to ping: subs first, else default schedule
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][today.weekday()]
    sched = _read_schedule_for_weekday(weekday)

    # (optional) validation of station names deferred by request

    # Build a message that pings the right people
    lines = await build_8pm_lines(bot, unfed=unfed, sched=sched, mention=True)

    # Use feeding team channel for alerts
    channel_id = getattr(settings, "ch_feeding_team", None)
    if not channel_id:
        log_action("feeding_8pm", "channel=None", "skipped (no alert channel configured)")
        return

    ch = bot.get_channel(int(channel_id))
    if not ch:
        log_action("feeding_8pm", f"channel={channel_id}", "not_found")
        return

    msg = lines
    from discord.abc import Messageable
    from ..utils.sender import safe_send

    try:
        if isinstance(ch, Messageable):
            await safe_send(ch, msg)  # silent mode respected here
            log_action("feeding_8pm", f"unfed={len(unfed)}", "sent")
        else:
            log_action("feeding_8pm", f"channel={channel_id}; type={type(ch).__name__}", "not_messageable")
    except Exception as e:
        log_action("feeding_8pm_error", f"unfed={len(unfed)}", str(e))

async def build_8pm_lines(
    bot: discord.Client,
    *,
    unfed: Optional[List[str]] = None,
    sched: Optional[Dict[str, List[int]]] = None,
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
        sched = _read_schedule_for_weekday(weekday)
    async with _SUBS_LOCK:
        files = _load_sub_files(_recent_month_keys(), include_legacy=os.path.exists(SUBS_LEGACY_FILE))
        subs: List[dict] = []
        for _, rows in files:
            subs.extend(rows)
    today_iso = today.isoformat()

    def _fmt(uid: int) -> str:
        if mention:
            return f"<@{uid}>"
        name = None
        if bot:
            u = bot.get_user(uid)
            if u:
                name = getattr(u, "global_name", None) or getattr(u, "display_name", None) or getattr(u, "name", None)
        if not name:
            try:
                lookup = getattr(settings, "user_id_map", {}) or {}
                for disp, mapped in lookup.items():
                    if int(mapped) == int(uid):
                        name = disp
                        break
            except Exception:
                pass
        if not name:
            name = str(uid)
        return str(name)

    sched = sched or {}

    def _assignees_for(station: str) -> List[int]:
        assignees: List[int] = []
        for r in reversed(subs):
            if r.get("station") == station and r.get("status") == "accepted" and today_iso in (r.get("dates") or []):
                aid = r.get("assignee")
                if isinstance(aid, int):
                    assignees.append(aid)
                    break
        if not assignees:
            assignees = sched.get(station, [])
        return assignees

    def _format_station_line(station: str) -> str:
        assignees = _assignees_for(station)
        if assignees:
            roster = " ".join(_fmt(uid) for uid in assignees)
        else:
            roster = "Unassigned."
        return f"• **{station}** → {roster}"

    if not include_fed:
        lines: List[str] = ["**Currently unfed stations**"]
        if not unfed:
            lines.append("• none")
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
        sections.append("• none")

    sections.append("**Unfed stations**")
    if unfed:
        for st in unfed:
            sections.append(_format_station_line(st))
    else:
        sections.append("• none")

    return "\n".join(sections)

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
