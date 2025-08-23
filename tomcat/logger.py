from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
from pathlib import Path

LOG_DIR_MACHINE = Path("logs/machine")
LOG_DIR_HUMAN = Path("logs/human")
LOG_DIR_MACHINE.mkdir(parents=True, exist_ok=True)
LOG_DIR_HUMAN.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo("America/Chicago")

def log_event(event_data: dict) -> str:
    """Write event_data to machine and human logs. Return human line."""
    # machine log always gets raw event with ts
    with open(LOG_DIR_MACHINE / f"{datetime.now(TZ):%Y-%m-%d}.ndjson", "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")

    # Human lines: format by type, never show ts
    ts_ct = datetime.now(TZ).strftime("%m/%d/%Y, %I:%M:%S %p")
    event = event_data.get("event", "event")

    if event == "message":
        content = event_data.get("content")
        if content is None or content == "":
            # Show a terse summary for non-text messages
            content = "(no text; attachments=" + str(event_data.get("attachments", 0)) + ")"
        human_line = (
            f"[{ts_ct}] Message | User: {event_data.get('author')} | "
            f"Channel: {event_data.get('channel')} | Content: {content}"
        )
    elif event == "action":
        human_line = (
            f"[{ts_ct}] {event_data.get('name')} | "
            f"Trigger: {event_data.get('trigger')} | Output: {event_data.get('output')}"
        )
    elif event == "online":
        human_line = f"[{ts_ct}] ONLINE | {event_data.get('user')} in {event_data.get('guild_count')} guild(s)"
    else:
        # Drop ts from the dump; keep other fields for debugging
        data_copy = dict(event_data)
        data_copy.pop("ts", None)
        human_line = f"[{ts_ct}] Event | " + json.dumps(data_copy, ensure_ascii=False)

    with open(LOG_DIR_HUMAN / f"{datetime.now(TZ):%Y-%m-%d}.log", "a", encoding="utf-8") as f:
        f.write(human_line + "\n")
    return human_line

def log_action(name: str, trigger: str, output: str) -> str:
    return log_event({
        "event": "action",
        "name": name,
        "trigger": trigger,
        "output": output,
    })
