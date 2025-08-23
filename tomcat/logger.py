from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_DIR_MACHINE = Path("logs/machine")
LOG_DIR_HUMAN = Path("logs/human")
LOG_DIR_MACHINE.mkdir(parents=True, exist_ok=True)
LOG_DIR_HUMAN.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo("America/Chicago")


def log_event(event_data: dict) -> str:
    """Write event_data to machine and human logs.
    Returns the human-readable line for optional Discord echo."""
    ts_utc = datetime.fromisoformat(event_data["ts"])
    ts_ct = ts_utc.astimezone(TZ)
    date_str = ts_ct.strftime("%Y-%m-%d")
    time_str = ts_ct.strftime("%m/%d/%Y, %I:%M:%S %p")

    # machine log
    with open(LOG_DIR_MACHINE / f"{date_str}.ndjson", "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")

    event = event_data.get("event", "event")
    human_line = ""
    if event == "message":
        human_line = (
            f"[{time_str}] Message | User: {event_data.get('author')} | "
            f"Channel: {event_data.get('channel')} | Content: {event_data.get('content')}"
        )
    elif event == "action":
        human_line = (
            f"[{time_str}] {event_data.get('name')} | Trigger: {event_data.get('trigger')} | "
            f"Output: {event_data.get('output')}"
        )
    elif event == "email_received":
        human_line = (
            f"[{time_str}] EMAIL_RECEIVED | From: {event_data.get('from')} | "
            f"Type: {event_data.get('type')}"
        )
    else:
        human_line = f"[{time_str}] Event | {json.dumps(event_data, ensure_ascii=False)}"

    with open(LOG_DIR_HUMAN / f"{date_str}.log", "a", encoding="utf-8") as f:
        f.write(human_line + "\n")

    return human_line


def log_action(name: str, trigger: str, output: str) -> str:
    return log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "action",
        "name": name,
        "trigger": trigger,
        "output": output,
    })
