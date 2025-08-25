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
from ..utils.sender import safe_send

# Optional TZ support
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # type: ignore

CENTRAL_TZ = ZoneInfo("America/Chicago") if ZoneInfo else None

# ------------- paths for local data -------------
DATA_DIR = os.path.join("data")
LOG_DIR = os.path.join("logging")
SUBS_FILE = os.path.join(LOG_DIR, "subs.jsonl")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

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
    users = _load_json(USERS_FILE, {})
    ids: List[int] = []
    for n in names:
        uid = users.get(n) or users.get(n.strip("@"))
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
    sched = _load_json(SCHEDULE_FILE, {})
    day = sched.get(weekday_name, {})
    out: Dict[str, List[int]] = {}
    for station, names in day.items():
        lst = names if isinstance(names, list) else [names]
        out[station] = _resolve_user_ids(lst)
    return out

# ------------- Google Sheets glue (safe stubs) ---
def _get_feeding_checklist_sheet_id() -> Optional[str]:
    # If you have this in settings, expose it here.
    return getattr(settings, "sheet_feeding_checklist_id", None) or getattr(settings, "sheet_feeding_id", None)

async def _mark_checkbox_in_sheet(station: str, date_iso: str) -> bool:
    """
    Wire this to your actual Google Sheets logic.
    For now, we log success and pretend it's done to avoid blocking development.
    """
    try:
        # TODO: integrate with sheets client:
        # from .sheets_client import get_client
        # gc = get_client()
        # sheet = gc.open_by_key(_get_feeding_checklist_sheet_id()).worksheet("FeedingStationChecklist")
        # ... find row for station and column for date ...
        # sheet.update_cell(row, col, True)
        log_action("sheet_mark", f"station={station} date={date_iso}", "ok (stub)")
        return True
    except Exception as e:
        log_action("sheet_mark_error", f"station={station} date={date_iso}", str(e))
        return False

async def _list_unfed_stations_today() -> List[str]:
    """
    Return a list of station names that have NOT been marked fed today.
    Replace stub with real Sheets read. If absent, return [] to avoid spam.
    """
    sheet_id = _get_feeding_checklist_sheet_id()
    if not sheet_id:
        log_action("unfed_list", "sheet=None", "skipped (no sheet configured)")
        return []
    try:
        # TODO: read the sheet and compute unfed; for now we return []
        return []
    except Exception as e:
        log_action("unfed_list_error", "sheet=err", str(e))
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
    await ch.send("\n".join(lines))


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

    # Optional: add a ✅ reaction to the message when not muted
    try:
        msg = ctx.get("message")
        if msg and hasattr(msg, "add_reaction"):
            await msg.add_reaction("✅")
    except Exception:
        pass

    status = "ok" if ok_all else "partial"
    log_action("feed_update", f"station={station}; dates={','.join(dates)}", status)

async def handle_sub_request_event(event, ctx: Dict[str, Any]) -> None:
    """
    Log a sub request locally and post a small accept/decline UI.
    Assumes event.station may be None and event.dates may be None.
    """
    rec = {
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

    # Build accept/decline view
    class SubView(discord.ui.View):
        def __init__(self, requester_id: int, station: Optional[str], dates: List[str]):
            super().__init__(timeout=3600)  # 1 hour window
            self.requester_id = requester_id
            self.station = station
            self.dates = dates

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            # requester cannot accept their own sub request
            if interaction.user and interaction.user.id == self.requester_id:
                await interaction.response.send_message("You can’t accept your own request.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="I can cover", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
            await _accept_latest_open_sub_in_channel(event.channel_id, interaction.user.id)
            try:
                await interaction.response.send_message("You’re marked as the sub. Thanks!", ephemeral=True)
            except Exception:
                pass
            self.stop()

        @discord.ui.button(label="Can’t", style=discord.ButtonStyle.secondary)
        async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
            try:
                await interaction.response.send_message("No worries.", ephemeral=True)
            except Exception:
                pass

    # Only send if not muted; if muted, log that we would have sent
    embed = discord.Embed(
        title="Sub request",
        description=f"Requester: <@{event.user_id}>\n"
                    f"Station: **{event.station or 'Unknown'}**\n"
                    f"Dates: {', '.join(event.dates or ['unspecified'])}",
        color=0x2F3136,
    )
    view = SubView(requester_id=event.user_id, station=event.station, dates=event.dates or [])

    try:
        await ctx["channel"].send(embed=embed, view=view)
        log_action("sub_request_ui", f"station={event.station}", "sent")
    except Exception:
        log_action("sub_request_ui", f"station={event.station}", "suppressed")

async def handle_sub_accept_event(event, ctx: Dict[str, Any]) -> None:
    """
    Someone said 'sure/I can cover'. Assign them to the most recent open request in this channel.
    """
    ok = await _accept_latest_open_sub_in_channel(event.channel_id, event.user_id)
    log_action("sub_accept", f"user={event.user_id}", "ok" if ok else "no_open_request")

# ------------- persistence for subs ------------
async def _accept_latest_open_sub_in_channel(channel_id: int, assignee_id: int) -> bool:
    rows = _read_jsonl(SUBS_FILE)
    # scan from bottom for requested in this channel
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        if r.get("channel_id") == channel_id and r.get("status") == "requested":
            r["status"] = "accepted"
            r["assignee"] = assignee_id
            r["updated_at"] = _now_iso()
            rows[i] = r
            _rewrite_jsonl(SUBS_FILE, rows)
            return True
    return False

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
    # compute unfed stations from sheet (stub now)
    unfed = await _list_unfed_stations_today()
    if not unfed:
        log_action("feeding_8pm", "unfed=0", "nothing_to_ping")
        return

    # choose who to ping: subs first, else default schedule
    today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
    weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][today.weekday()]
    sched = _read_schedule_for_weekday(weekday)

    # Build a message that pings the right people
    lines: List[str] = ["**Unfed stations today**"]
    mentions: List[str] = []
    subs = _read_jsonl(SUBS_FILE)
    today_iso = today.isoformat()

    for st in unfed:
        # Is there an accepted sub for today for this station?
        assignees: List[int] = []
        for r in reversed(subs):
            if r.get("station") == st and r.get("status") == "accepted" and today_iso in (r.get("dates") or []):
                aid = r.get("assignee")
                if isinstance(aid, int):
                    assignees.append(aid)
                    break
        # fallback to default schedule
        if not assignees:
            assignees = sched.get(st, [])
        if assignees:
            m = " ".join(f"<@{uid}>" for uid in assignees)
            mentions.append(m)
            lines.append(f"• **{st}** → {m}")
        else:
            lines.append(f"• **{st}** → _(no one assigned)_")

    channel_id = getattr(settings, "feeding_alert_channel_id", None)
    if not channel_id:
        log_action("feeding_8pm", "channel=None", "skipped (no alert channel configured)")
        return

    ch = bot.get_channel(int(channel_id))
    if not ch:
        log_action("feeding_8pm", f"channel={channel_id}", "not_found")
        return

    msg = "\n".join(lines)
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

