from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
from pathlib import Path

LOG_DIR_MACHINE = Path("logs/machine")
LOG_DIR_HUMAN = Path("logs/human")
LOG_DIR_MACHINE.mkdir(parents=True, exist_ok=True)
LOG_DIR_HUMAN.mkdir(parents=True, exist_ok=True)

from typing import Any

TZ = ZoneInfo("America/Chicago")

_COLW = {"event": 8, "col1": 25, "col2": 45}

def _pad(s: str, width: int) -> str:
    s = str(s or "")
    return s if len(s) >= width else s + (" " * (width - len(s)))

def _human_line(ts_ct: str, event: str, col1: str = "", col2: str = "", tail: str = "") -> str:
    head = f"[{ts_ct}] " + " || ".join([
        _pad(event, _COLW["event"]),
        _pad(col1, _COLW["col1"]),
        _pad(col2, _COLW["col2"]),
    ])
    return head + ((" || " + tail) if tail else "")


def log_event(event_data: dict) -> str:
    # Write machine log (raw NDJSON)
    with open(LOG_DIR_MACHINE / f"{datetime.now(TZ):%Y-%m-%d}.ndjson", "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")
    
    now = datetime.now(TZ)
    ts_ct = f"{now:%m/%d/%Y %I:%M:%S}.{now.microsecond//1000:03d} {'AM' if now.hour < 12 else 'PM'}"

    kind = str(event_data.get("event", "event")).lower()

    if kind == "message":
        content = event_data.get("content")
        if content is None or content == "":
            content = "(no text; attachments=" + str(event_data.get("attachments", 0)) + ")"
        human_line = _human_line(
            ts_ct,
            "Message",
            f"User: {event_data.get('author','')}",
            f"Channel: {event_data.get('channel','')}",
            f"Content: {content}",
        )
    elif kind == "action":
        human_line = _human_line(
            ts_ct,
            "Action",
            f"Name: {event_data.get('name','')}",
            f"Trigger: {event_data.get('trigger','')}",
            f"Output: {event_data.get('output','')}",
        )
    elif kind == "online":
        human_line = _human_line(
            ts_ct,
            "Online",
            f"User: {event_data.get('user','')}",
            f"Guilds: {event_data.get('guild_count','')}",
            "",
        )
    else:
        data_copy = dict(event_data)
        data_copy.pop("ts", None)
        human_line = _human_line(ts_ct, "Event", "", "", json.dumps(data_copy, ensure_ascii=False))

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

def log_intent(kind: str, confidence: float, **extras: Any) -> str:
    return log_event({"event": "intent", "kind": kind, "confidence": round(float(confidence), 3), **(extras or {})})