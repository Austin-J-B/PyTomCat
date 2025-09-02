# tomcat/feeding.py
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

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
# Single append-only JSONL file under logs/subs
SUBS_DIR = os.path.join("logs", "subs")
os.makedirs(SUBS_DIR, exist_ok=True)
SUBS_FILE = os.path.join(SUBS_DIR, "subs.jsonl")

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
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    return now.date().isoformat()

def _now_iso() -> str:
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    return now.isoformat()

# ------------- helpers: files/json --------------
def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _append_jsonl(path: str, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")

def _read_jsonl(path: str) -> List[dict]:
    out: List[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out

def _rewrite_jsonl(path: str, rows: List[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

# ------------- helpers: schedule/users ----------
def _resolve_user_ids(names: List[str]) -> List[int]:
    """Resolve a list of display names to Discord user IDs via settings.user_id_map.
    Accepts either names or numeric strings.
    """
    cfg_map = getattr(settings, "user_id_map", {}) or {}
    # normalize keys to simple form
    norm_map = {str(k).strip(): int(v) for k, v in cfg_map.items() if str(v).isdigit() or isinstance(v, int)}
    ids: List[int] = []
    for n in names:
        n1 = str(n).strip()
        uid = norm_map.get(n1) or norm_map.get(n1.strip("@"))
        if isinstance(uid, int):
            ids.append(uid)
        else:
            # allow numeric strings too
            try:
                ids.append(int(str(uid)))
            except Exception:
                pass
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
        if not isinstance(seq, list) or not seq:
            continue
        if len(seq) != 7:
            log_action("schedule_warn", f"station={station}", f"len={len(seq)} != 7; cycling")
        name = seq[idx % len(seq)]
        ids = _resolve_user_ids([name]) if name is not None else []
        out[station] = ids
    return out

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

async def _mark_checkbox_in_sheet(station: str, date_iso: str) -> bool:
    """Mark the (station, date) cell TRUE in the FeedingStationChecklist tab.
    Header row (1) has stations; first column (A) has dates; body is checkboxes.
    """
    ws = _open_feeding_ws()
    if ws is None:
        return False
    try:
        header = _station_header_map(ws)
        # if station isn't exact, try resolving via aliases
        disp = station
        if disp not in header:
            resolved = resolve_station_or_cat(station, want="station")
            if resolved and resolved in header:
                disp = resolved
        col = header.get(disp)
        row = _find_date_row(ws, date_iso)
        if not col or not row:
            log_action("sheet_mark_error", f"station={station} date={date_iso}", "missing_row_or_col")
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
        row = _find_date_row(ws, today_iso)
        if not row:
            log_action("unfed_list", f"date={today_iso}", "date_row_not_found")
            # If date absent, treat all as unfed to be safe
            return [name for name, col in header.items() if col != 1]
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
                unfed.append(name)
        return unfed
    except Exception as e:
        log_action("unfed_list_error", "read", str(e))
        return []

async def handle_feeding_inquiry(intent, ctx: Dict[str, Any]) -> None:
    ch = ctx["channel"]
    # Get today’s stations from your schedule (fallback to keys union if needed)
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
    lines.append("**Feeding status (today)**")
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
    station = event.station or "Unknown"
    dates = event.dates or [_today_iso()]

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
    rec = {
        "kind": "sub_request",
        "id": f"sub-{event.message_id}",
        "station": event.station,
        "dates": event.dates or [],
        "requester": event.user_id,
        "assignee": None,
        "status": "requested",
        "channel_id": event.channel_id,
        "message_id": event.message_id,
        "created_at": _now_iso(),
    }
    _append_jsonl(SUBS_FILE, rec)
    log_action("sub_request", f"station={event.station}; dates={event.dates}", "logged")

    # No UI; subs are fully silent by design

async def handle_sub_accept_event(event, ctx: Dict[str, Any]) -> None:
    """
    Someone said 'sure/I can cover'. Assign them to the most recent open request in this channel.
    """
    accepted_id = await _accept_latest_open_sub_in_channel(event.channel_id, event.user_id)
    if accepted_id:
        _append_jsonl(SUBS_FILE, {
            "kind": "sub_accept",
            "sub_id": accepted_id,
            "assignee": event.user_id,
            "channel_id": event.channel_id,
            "message_id": event.message_id,
            "ts": _now_iso(),
        })
        log_action("sub_accept", f"user={event.user_id}", "ok")
    else:
        log_action("sub_accept", f"user={event.user_id}", "no_open_request")

# ------------- persistence for subs ------------
async def _accept_latest_open_sub_in_channel(channel_id: int, assignee_id: int) -> Optional[str]:
    rows = _read_jsonl(SUBS_FILE)
    # scan from bottom for requested in this channel
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        if r.get("channel_id") == channel_id and r.get("status") == "requested":
            r["status"] = "accepted"
            r["assignee"] = assignee_id
            # If no dates were specified, assume today
            if not r.get("dates"):
                r["dates"] = [ _today_iso() ]
            r["updated_at"] = _now_iso()
            rows[i] = r
            _rewrite_jsonl(SUBS_FILE, rows)
            return str(r.get("id")) if r.get("id") else None
    return None

# ------------- scheduler: 8:00 pm ping ----------
async def start_feeding_scheduler(bot: discord.Client) -> None:
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
    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

async def _run_8pm_check(bot: discord.Client) -> None:
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

async def build_8pm_lines(bot: discord.Client, *, unfed: Optional[List[str]] = None, sched: Optional[Dict[str, List[int]]] = None, mention: bool = True) -> str:
    """Build the text for the 8pm message. mention=True uses <@id> tags; else shows @username/ID.
    If unfed/sched not provided, computes them.
    """
    if unfed is None:
        unfed = await _list_unfed_stations_today()
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    if sched is None:
        weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][today.weekday()]
        sched = _read_schedule_for_weekday(weekday)
    subs = _read_jsonl(SUBS_FILE)
    today_iso = today.isoformat()

    def _fmt(uid: int) -> str:
        if mention:
            return f"<@{uid}>"
        u = bot.get_user(uid)
        return f"@{getattr(u,'name',str(uid))}"

    lines: List[str] = ["**Currently unfed stations**"]
    for st in unfed:
        # accepted sub for today?
        assignees: List[int] = []
        for r in reversed(subs):
            if r.get("station") == st and r.get("status") == "accepted" and today_iso in (r.get("dates") or []):
                aid = r.get("assignee")
                if isinstance(aid, int):
                    assignees.append(aid)
                    break
        if not assignees:
            assignees = sched.get(st, [])
        if assignees:
            lines.append(f"• **{st}** → {' '.join(_fmt(uid) for uid in assignees)}")
        else:
            lines.append(f"• **{st}** → Unassigned.")
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

