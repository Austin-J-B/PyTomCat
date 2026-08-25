"""Structured logging helpers write events to the machine log.

Machine ndjson is the only log; the human-readable log was removed
deliberately (see "Death to 'human' logs"). Every record carries a "ts".
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
from pathlib import Path

LOG_DIR_MACHINE = Path("logs/machine")
LOG_DIR_MACHINE.mkdir(parents=True, exist_ok=True)

from typing import Any

TZ = ZoneInfo("America/Chicago")


def log_event(event_data: dict) -> None:
    """Write event to machine log (ndjson)."""
    now_dt = datetime.now(TZ)
    month_dir = LOG_DIR_MACHINE / f"{now_dt:%Y-%m}"
    month_dir.mkdir(parents=True, exist_ok=True)
    #"ts" leads every record so a line is self-describing and sorts naturally.
    #Without it the only time information was the filename, which pins an event
    #to a day but not to a minute -- enough to say something happened, not
    #enough to line a user report up with the traceback that caused it.
    #Local time with an explicit offset, matching the file naming above; the
    #offset keeps it unambiguous against UTC sources like journalctl.
    #A caller that supplies its own "ts" (e.g. replaying an earlier event)
    #overrides this one.
    record = {"ts": now_dt.isoformat(timespec="milliseconds"), **event_data}
    with open(month_dir / f"{now_dt:%Y-%m-%d}.ndjson", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_action(name: str, trigger: str, output: str) -> None:
    """Emit an action log entry."""
    log_event({
        "event": "action",
        "name": name,
        "trigger": trigger,
        "output": output,
    })

def log_intent(kind: str, confidence: float, **extras: Any) -> None:
    """Shortcut for logging high-level intent classification results."""
    log_event({"event": "intent", "kind": kind, "confidence": round(float(confidence), 3), **(extras or {})})
