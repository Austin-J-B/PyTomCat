"""Scheduled 4AM gallery retrain orchestration (opt-in via UI)."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from ..config import settings
from ..logger import log_action
from .gallery_updater import run_gallery_update

_STATE_PATH = Path("cache") / "gallery_retrain" / "schedule.json"
_LOCK_DIR = Path("logs") / "gallery_retrain" / "locks"
_SCHEDULER_LOCK = asyncio.Lock()
_RUN_LOCK = asyncio.Lock()
_STARTED = False
_LAST_RUN_KEY: Optional[str] = None


def _tz_now() -> datetime:
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(getattr(settings, "timezone", "America/Chicago"))
        except Exception:
            tz = None
    return datetime.now(tz) if tz else datetime.now()


def _default_state() -> Dict[str, Any]:
    return {
        "enabled": False,
        "scheduled_date": None,   # YYYY-MM-DD (local timezone)
        "requested_at": None,
        "requested_by": None,
        "last_run_date": None,
        "last_run_at": None,
        "last_result": None,
    }


def _load_state() -> Dict[str, Any]:
    st = _default_state()
    try:
        if _STATE_PATH.exists():
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                st.update(raw)
    except Exception:
        pass
    return st


def _save_state(state: Dict[str, Any]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(_STATE_PATH)


def _next_4am_date_iso(now_local: datetime) -> str:
    target = now_local.replace(hour=4, minute=0, second=0, microsecond=0)
    if target <= now_local:
        target = target + timedelta(days=1)
    return target.date().isoformat()


def _target_dt(date_iso: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(date_iso)).date()
    except Exception:
        return None
    now = _tz_now()
    return now.replace(year=d.year, month=d.month, day=d.day, hour=4, minute=0, second=0, microsecond=0)


def _run_lock_path(run_key: str) -> Path:
    safe = "".join(ch for ch in str(run_key) if ch.isdigit() or ch == "-")
    return _LOCK_DIR / f"{safe}.lock"


def _acquire_run_lock(run_key: str) -> bool:
    try:
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
        p = _run_lock_path(run_key)
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(_tz_now().isoformat()).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        # If lock creation fails unexpectedly, fall back to in-process guard.
        return True


async def get_gallery_retrain_status() -> Dict[str, Any]:
    st = _load_state()
    st["running"] = _RUN_LOCK.locked()
    date_iso = st.get("scheduled_date")
    target = _target_dt(date_iso) if date_iso else None
    st["target_at"] = target.isoformat() if target else None
    return st


async def schedule_gallery_retrain(*, requested_by: str) -> Dict[str, Any]:
    now = _tz_now()
    sched_date = _next_4am_date_iso(now)
    async with _SCHEDULER_LOCK:
        st = _load_state()
        st["enabled"] = True
        st["scheduled_date"] = sched_date
        st["requested_at"] = now.isoformat()
        st["requested_by"] = requested_by or "unknown"
        _save_state(st)
    log_action("gallery_retrain_schedule", f"date={sched_date}", f"by={requested_by}")
    return await get_gallery_retrain_status()


async def _run_if_due() -> None:
    global _LAST_RUN_KEY
    st = _load_state()
    if not st.get("enabled"):
        return
    run_key = str(st.get("scheduled_date") or "").strip()
    if not run_key:
        return

    target = _target_dt(run_key)
    if target is None:
        return
    now = _tz_now()
    if now < target:
        return

    if _LAST_RUN_KEY == run_key:
        return
    if _RUN_LOCK.locked():
        return

    if not _acquire_run_lock(run_key):
        # Another process already started/completed this date; mark as consumed.
        async with _SCHEDULER_LOCK:
            st2 = _load_state()
            st2["enabled"] = False
            st2["last_run_date"] = run_key
            st2["last_run_at"] = _tz_now().isoformat()
            st2["last_result"] = {"status": "duplicate_skip_lock"}
            _save_state(st2)
        _LAST_RUN_KEY = run_key
        log_action("gallery_retrain_scheduler", f"date={run_key}", "duplicate_skip_lock")
        return

    async with _RUN_LOCK:
        _LAST_RUN_KEY = run_key
        result: Dict[str, Any]
        try:
            result = await asyncio.to_thread(run_gallery_update, mode="full")
        except Exception as e:
            result = {"status": "error", "error": str(e)}
            log_action("gallery_retrain_scheduler_error", f"date={run_key}", str(e))

        async with _SCHEDULER_LOCK:
            st3 = _load_state()
            st3["enabled"] = False
            st3["scheduled_date"] = None
            st3["last_run_date"] = run_key
            st3["last_run_at"] = _tz_now().isoformat()
            st3["last_result"] = result
            _save_state(st3)

        log_action(
            "gallery_retrain_scheduler",
            f"date={run_key}",
            str(result.get("status", "ok")),
        )


async def start_gallery_retrain_scheduler() -> None:
    """Background loop: execute opt-in retrain once when local time reaches 4:00 AM."""
    global _STARTED
    async with _SCHEDULER_LOCK:
        if _STARTED:
            log_action("gallery_retrain_scheduler", "already_started", "skipped")
            return
        _STARTED = True

    while True:
        try:
            await _run_if_due()
        except Exception as e:
            log_action("gallery_retrain_scheduler_error", "loop", str(e))
        await asyncio.sleep(20)
