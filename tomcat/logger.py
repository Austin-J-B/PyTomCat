"""Structured logging helpers write events to the machine log.

Machine ndjson is the only log; the human-readable log was removed
deliberately (see "Death to 'human' logs"). Every record carries a "ts".
"""

from datetime import datetime
from zoneinfo import ZoneInfo
import atexit
import json
import threading
from pathlib import Path
from typing import Any, Optional, TextIO

LOG_DIR_MACHINE = Path("logs/machine")
LOG_DIR_MACHINE.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo("America/Chicago")

#The day's log file stays open. Every event used to cost a mkdir plus an
#open/close pair, which is a handful of syscalls on the event loop for the
#busiest thing the bot does. The handle is line-buffered and opened for append,
#so each record still reaches the OS as one atomic write and nothing is lost if
#the process dies; only log_retention deletes files, and never the current one.
_LOG_LOCK = threading.Lock()
_log_day: Optional[str] = None
_log_handle: Optional[TextIO] = None


def _close_log_handle() -> None:
    global _log_handle, _log_day
    handle, _log_handle, _log_day = _log_handle, None, None
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def _handle_for_day(day: str, month: str) -> TextIO:
    """The append handle for `day`, rotating when the date rolls over."""
    global _log_handle, _log_day
    if _log_handle is not None and _log_day == day:
        return _log_handle
    _close_log_handle()
    month_dir = LOG_DIR_MACHINE / month
    month_dir.mkdir(parents=True, exist_ok=True)
    _log_handle = open(month_dir / f"{day}.ndjson", "a", encoding="utf-8", buffering=1)
    _log_day = day
    return _log_handle


atexit.register(_close_log_handle)


def log_event(event_data: dict) -> None:
    """Write event to machine log (ndjson)."""
    now_dt = datetime.now(TZ)
    #"ts" leads every record so a line is self-describing and sorts naturally.
    #Without it the only time information was the filename, which pins an event
    #to a day but not to a minute -- enough to say something happened, not
    #enough to line a user report up with the traceback that caused it.
    #Local time with an explicit offset, matching the file naming above; the
    #offset keeps it unambiguous against UTC sources like journalctl.
    #A caller that supplies its own "ts" (e.g. replaying an earlier event)
    #overrides this one.
    record = {"ts": now_dt.isoformat(timespec="milliseconds"), **event_data}
    line = json.dumps(record, ensure_ascii=False) + "\n"
    day = f"{now_dt:%Y-%m-%d}"
    month = f"{now_dt:%Y-%m}"
    with _LOG_LOCK:
        try:
            _handle_for_day(day, month).write(line)
        except OSError:
            #A stale handle (a moved directory, a full disk that has since been
            #cleared) must not silently drop the rest of the run's logs.
            _close_log_handle()
            _handle_for_day(day, month).write(line)


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
