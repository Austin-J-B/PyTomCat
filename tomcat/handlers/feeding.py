from __future__ import annotations
import asyncio
import time
import discord
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Fixed: Use ZoneInfo for IANA strings

from ..config import settings
from ..logger import log_action
from ..services.sheets_client import sheets_client
from ..services.scheduler_store import load_schedule

# --- CONSTANTS ---
FEEDING_TIMES = {
    "8PM": (20, 0),
}
TOLERANCE_MINUTES = 5

# Map display names to sheet column headers if they differ, 
# otherwise assumes 1:1 mapping.
STATION_COLUMNS = {
    "Microwave": "Microwave",
    "Snickers": "Snickers",
    "Business": "Business",
    "The Greens": "The Greens",
    "HOP": "HOP",
    "Lot 50": "Lot 50",
    "Mary Kay and Zen": "Mary Kay & Zen", # Note the '&' vs 'and' difference often found in sheets
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
        # Assumes the tab is named 'Feeding' or similar where checkmarks live
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
            # FIXED: Use ZoneInfo for string timezones like 'America/Chicago'
            tz = ZoneInfo(settings.timezone)
            now = datetime.now(tz)
            
            target_h, target_m = FEEDING_TIMES["8PM"]
            
            is_time = (now.hour == target_h and 
                       target_m <= now.minute < target_m + TOLERANCE_MINUTES)
            
            if is_time:
                await _run_pings(bot, now)
                # Sleep 12 hours to prevent double pinging today
                await asyncio.sleep(12 * 3600)
            else:
                # Check every minute
                await asyncio.sleep(60)
        except Exception as e:
            print(f"[FeedingScheduler] Critical Error: {e}")
            await asyncio.sleep(60) # Don't crash the loop

async def _run_pings(bot, now_dt):
    """Execute the daily pings based on JSON schedule."""
    day_idx = int(now_dt.strftime("%w")) # 0=Sunday
    
    # Load from JSON
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
    await channel.send(msg)

async def handle_feeding_inquiry(intent, ctx):
    """
    Checks the Google Sheet to see if a station has been marked as fed today.
    Triggered by: "Has Snickers been fed?"
    """
    channel = ctx["channel"]
    # 1. Identify station from intent data or text
    station_query = intent.data.get("station_name") or "unknown"
    
    # Simple fuzzy match or direct map lookup
    target_station = None
    for db_name, sheet_col in STATION_COLUMNS.items():
        if db_name.lower() in station_query.lower():
            target_station = sheet_col
            break
    
    if not target_station:
        await channel.send("I'm not sure which station you're asking about.")
        return

    await channel.send(f"Checking the log for **{target_station}**...")

    try:
        ws = _open_feeding_ws()
        if not ws:
            await channel.send("I couldn't access the feeding log.")
            return

        # Get all values to find today's row
        # Assuming Column A is 'Date' and rows are days
        rows = ws.get_all_values()
        headers = rows[0]
        
        # Find column index for the station
        try:
            col_idx = headers.index(target_station)
        except ValueError:
            await channel.send(f"I couldn't find a column for {target_station} in the sheet.")
            return

        # Find row index for today
        tz = ZoneInfo(settings.timezone)
        today_str = datetime.now(tz).strftime("%Y-%m-%d") # Adjust format to match your sheet!
        # Fallback: often sheets use M/D/YYYY or similar. 
        # Heuristic: check the last few rows.
        
        status = "Unknown"
        found_row = None
        
        # Search from bottom up for efficient "today" check
        for row in reversed(rows):
            # Check first column for date (adjust if date is elsewhere)
            if len(row) > 0 and (today_str in row[0] or datetime.now(tz).strftime("%-m/%-d") in row[0]):
                found_row = row
                break
        
        if found_row and len(found_row) > col_idx:
            val = found_row[col_idx].strip().lower()
            if val in ["x", "yes", "done", "fed", "check"]:
                status = "Fed ✅"
            elif val:
                status = f"Marked as: {val}"
            else:
                status = "Not fed yet ❌"
        else:
            status = "No entry found for today."

        await channel.send(f"**{target_station}** status for today: {status}")

    except Exception as e:
        log_action("feeding_inquiry_error", "check_sheet", str(e))
        await channel.send("Something went wrong checking the sheet.")