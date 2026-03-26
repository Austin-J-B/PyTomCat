"""Structured logging helpers fan messages to disk and stdout."""

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
    with open(month_dir / f"{now_dt:%Y-%m-%d}.ndjson", "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")


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
