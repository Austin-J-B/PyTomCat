from __future__ import annotations
import asyncio
import time
import discord
from datetime import datetime, timezone, timedelta

from ..config import settings
from ..logger import log_action
from ..services.sheets_client import sheets_client
# Import the new loader
from ..services.scheduler_store import load_schedule

# --- CONSTANTS ---
FEEDING_TIMES = {
    "8PM": (20, 0),
}
# 5-minute tolerance for the scheduler loop
TOLERANCE_MINUTES = 5

def _open_feeding_ws():
    """Helper to open the specific 'Feeding' tab for logging."""
    try:
        gc = sheets_client()
        if not settings.sheet_catabase_id:
            return None
        sh = gc.open_by_key(settings.sheet_catabase_id)
        # Adjust the worksheet title if necessary
        return sh.worksheet("Feeding")
    except Exception as e:
        log_action("sheet_error", "feeding_tab_open", str(e))
        return None

async def start_feeding_scheduler(bot):
    """Background loop to ping feeders at 8PM."""
    print("[FeedingScheduler] Starting loop...")
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now(timezone(settings.timezone))
        
        # Check for 8PM slot
        target_h, target_m = FEEDING_TIMES["8PM"]
        
        # We only trigger if current time is within [target, target + tolerance]
        # and we haven't already triggered today (naive check via sleep duration).
        
        is_time = (now.hour == target_h and 
                   target_m <= now.minute < target_m + TOLERANCE_MINUTES)
        
        if is_time:
            try:
                await _run_pings(bot, now)
            except Exception as e:
                log_action("feeding_scheduler_error", "run_pings", str(e))
            
            # Sleep until tomorrow to avoid double-pinging
            # Just sleep 12 hours to be safe, then loop resumes
            await asyncio.sleep(12 * 3600)
        else:
            # Check every minute
            await asyncio.sleep(60)

async def _run_pings(bot, now_dt):
    """Execute the daily pings."""
    day_idx = int(now_dt.strftime("%w")) # 0=Sunday, 6=Saturday
    
    # LOAD FROM JSON NOW
    current_schedule = load_schedule()
    
    user_map = settings.user_id_map
    
    # Identify the channel
    ch_id = settings.ch_feeding_team or settings.ch_sandbox
    if not ch_id:
        print("[FeedingScheduler] No feeding channel configured.")
        return
        
    channel = bot.get_channel(ch_id)
    if not channel:
        return

    # Build the ping list
    mentions = []
    assignments = []

    for station, volunteers in current_schedule.items():
        if not volunteers or len(volunteers) <= day_idx:
            continue
            
        feeder_name = volunteers[day_idx]
        if not feeder_name:
            continue
            
        assignments.append(f"**{station}**: {feeder_name}")
        
        # Resolve Discord ID
        uid = user_map.get(feeder_name)
        if uid:
            mentions.append(f"<@{uid}>")
        else:
            # Just text if we can't ping
            pass

    if not assignments:
        return

    # Construct message
    date_str = now_dt.strftime("%A, %b %d")
    msg_content = (
        f" **Feeding Call — {date_str}** 🐟\n\n"
        + "\n".join(assignments) + "\n\n"
        + " ".join(set(mentions)) # dedup pings
        + "\n\n* react ✅ when done*"
    )

    # Send
    try:
        sent = await channel.send(msg_content)
        await sent.add_reaction("✅")
    except Exception as e:
        log_action("feeding_ping_error", f"channel={ch_id}", str(e))

# --- Handler for "Has X been fed?" ---
async def handle_feeding_inquiry(intent, ctx):
    """
    Responds to 'has Snickers been fed?' logic. 
    For now, this just checks the Google Sheet log or active messages.
    Simplified placeholder logic here.
    """
    channel = ctx["channel"]
    await channel.send("error[01]")