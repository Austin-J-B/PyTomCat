import os
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_DIR_MACHINE = Path("logs/machine")
LOG_DIR_HUMAN = Path("logs/human")
LOG_DIR_MACHINE.mkdir(parents=True, exist_ok=True)
LOG_DIR_HUMAN.mkdir(parents=True, exist_ok=True)

def log_event(event_data):
    ts_utc = datetime.fromisoformat(event_data["ts"])
    # Convert to Central Time for human-readable log
    ts_ct = ts_utc.astimezone(ZoneInfo("America/Chicago"))
    
    # Keep filenames based on UTC date for consistency
    date_str = ts_utc.strftime("%Y-%m-%d")
    time_str = ts_ct.strftime("%m/%d/%Y, %I:%M:%S %p")

    # Machine log (NDJSON) - remains in UTC
    with open(LOG_DIR_MACHINE / f"{date_str}.ndjson", "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")

    # Human-readable log
    if event_data["event"] == "ready":
        human_line = f"[{time_str}] System: TomCat Online\n"
    elif event_data["event"] == "message":
        human_line = f"[{time_str}] {event_data['author']} in channel: {event_data['channel']}: {event_data['content']}\n"
    elif event_data["event"] == "member_join":
        invite_str = f" via {event_data.get('invite_code', 'unknown invite')}"
        human_line = f"[{time_str}] MemberJoin: {event_data['name']}{invite_str}\n"
    elif event_data["event"] in ("reaction_add", "reaction_remove"):
        action = "added" if event_data["event"] == "reaction_add" else "removed"
        user_display = event_data.get('user_name', f"User {event_data['user_id']}")
        human_line = f"[{time_str}] Reaction: {user_display} {action} {event_data['emoji']} on message {event_data['message_id']}\n"
    elif event_data["event"] == "message_edit":
        human_line = f"[{time_str}] MessageEdit: ID {event_data['id']}\n  Before: {event_data['before']}\n  After:  {event_data['after']}\n"
    elif event_data["event"] == "message_delete":
        human_line = f"[{time_str}] MessageDelete: ID {event_data['id']}\n"
    elif event_data["event"] == "intent":
        human_line = f"[{time_str}] Intent: Matched '{event_data['type']}' with args {event_data['args']} from message {event_data['msg_id']}\n"
    else:
        human_line = f"[{time_str}] Event: {event_data['event']} — {json.dumps(event_data)}\n"

    with open(LOG_DIR_HUMAN / f"{date_str}.log", "a", encoding="utf-8") as f:
        f.write(human_line)
