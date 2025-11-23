from __future__ import annotations
import asyncio
import time
import discord
from datetime import datetime, timedelta
# FIX: Use ZoneInfo to handle string timezones like 'America/Chicago'
from zoneinfo import ZoneInfo  

from ..config import settings
from ..logger import log_action
from ..services.sheets_client import sheets_client
from ..services.scheduler_store import load_schedule

# --- CONSTANTS ---
FEEDING_TIMES = {
    "8PM": (20, 0),
}
TOLERANCE_MINUTES = 5

# Map display names to sheet column headers
STATION_COLUMNS = {
    "Microwave": "Microwave",
    "Snickers": "Snickers",
    "Business": "Business",
    "The Greens": "The Greens",
    "HOP": "HOP",
    "Lot 50": "Lot 50",
    "Mary Kay and Zen": "Mary Kay & Zen", 
    "West Hall": "West Hall",
    "Maintenance": "Maintenance"
}

def _open_feeding_ws():
    """Helper to open the specific 'Feeding' tab for logging/checking."""
    try:
        gc = sheets_client()
        if not settings.sheet_catabase_id:
            return None
        sh = gc.open_by_key(settings.sheet_catabase_id)
        return sh.worksheet("Feeding")
    except Exception as e:
        log_action("sheet_error", "feeding_tab_open", str(e))
        return None

async def start_feeding_scheduler(bot):
    """Background loop to ping feeders at 8PM."""
    print("[FeedingScheduler] Starting loop...")
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            # FIX: Use ZoneInfo here instead of timezone()
            tz = ZoneInfo(settings.timezone)
            now = datetime.now(tz)
            
            target_h, target_m = FEEDING_TIMES["8PM"]
            
            is_time = (now.hour == target_h and 
                       target_m <= now.minute < target_m + TOLERANCE_MINUTES)
            
            if is_time:
                await _run_pings(bot, now)
                # Sleep 12 hours to prevent double pinging
                await asyncio.sleep(12 * 3600)
            else:
                await asyncio.sleep(60)
        except Exception as e:
            print(f"[FeedingScheduler] Loop Error: {e}")
            await asyncio.sleep(60)

async def _run_pings(bot, now_dt):
    """Execute the daily pings based on JSON schedule."""
    day_idx = int(now_dt.strftime("%w")) # 0=Sunday
    
    current_schedule = load_schedule()
    user_map = settings.user_id_map
    
    ch_id = settings.ch_feeding_team or settings.ch_sandbox
    if not ch_id: return
    channel = bot.get_channel(ch_id)
    if not channel: return

    assignments = []
    mentions = []

    for station, volunteers in current_schedule.items():
        if not volunteers or len(volunteers) <= day_idx: continue
        feeder = volunteers[day_idx]
        if not feeder: continue
        
        assignments.append(f"**{station}**: {feeder}")
        
        if feeder in user_map:
            mentions.append(f"<@{user_map[feeder]}>")

    if not assignments: return

    date_str = now_dt.strftime("%A, %b %d")
    msg = (
        f"🍎 **Feeding Call — {date_str}** 🐟\n\n"
        + "\n".join(assignments) + "\n\n"
        + " ".join(set(mentions))
        + "\n\n*Please react ✅ when done!*"
    )
    try:
        sent = await channel.send(msg)
        await sent.add_reaction("✅")
    except Exception: pass

async def handle_feeding_inquiry(intent, ctx):
    """
    Checks the Google Sheet to see if a station has been marked as fed today.
    """
    channel = ctx["channel"]
    station_query = intent.data.get("station_name") or "unknown"
    
    target_station = None
    for db_name, sheet_col in STATION_COLUMNS.items():
        if db_name.lower() in station_query.lower():
            target_station = sheet_col
            break
    
    if not target_station:
        await channel.send("I'm not sure which station you're asking about.")
        return

    status_msg = await channel.send(f"Checking the log for **{target_station}**...")

    try:
        ws = _open_feeding_ws()
        if not ws:
            await status_msg.edit(content="I couldn't access the feeding log.")
            return

        rows = ws.get_all_values()
        if not rows:
            await status_msg.edit(content="The feeding log appears empty.")
            return

        headers = rows[0]
        try:
            col_idx = headers.index(target_station)
        except ValueError:
            await status_msg.edit(content=f"Column '{target_station}' not found in sheet.")
            return

        # Check 'Date' column (usually col 0) for today
        tz = ZoneInfo(settings.timezone)
        today_dates = [
            datetime.now(tz).strftime("%Y-%m-%d"),
            datetime.now(tz).strftime("%-m/%-d"),
            datetime.now(tz).strftime("%m/%d/%Y")
        ]
        
        found_val = None
        # Scan from bottom up
        for row in reversed(rows):
            if not row: continue
            date_cell = row[0]
            if any(d in date_cell for d in today_dates):
                if len(row) > col_idx:
                    found_val = row[col_idx].strip()
                break
        
        if found_val and found_val.lower() in ["x", "yes", "done", "fed", "check", "true"]:
            resp = f"✅ **{target_station}** has been fed today."
        elif found_val:
            resp = f"ℹ️ **{target_station}** status: {found_val}"
        else:
            resp = f"❌ **{target_station}** has NOT been fed yet today."

        await status_msg.edit(content=resp)

    except Exception as e:
        log_action("feeding_inquiry_error", "check_sheet", str(e))
        await status_msg.edit(content="Error checking feeding status.")