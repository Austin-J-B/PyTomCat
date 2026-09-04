"""API endpoints for the web-based image labeling tool.

Routes:
  GET  /api/labeler/queue/detect    - Serials needing detector labels
  GET  /api/labeler/queue/classify  - Serials needing classifier labels
  GET  /api/labeler/image/<sn>      - Image + existing annotations
  GET  /api/labeler/cached_image/<sn> - Cached image bytes (fast)
  POST /api/labeler/detect          - Run YOLO+SAM â†’ boxes
  POST /api/labeler/identify        - Run DINOv3 â†’ top-N candidates
  POST /api/labeler/save            - Batch save annotations to local metadata
  POST /api/labeler/flag_incorrect  - Clear labels for one serial for relabel
  GET  /api/labeler/cats            - List all cat names for dropdown
"""
from __future__ import annotations
import io
import re
import os
import time
import json
import random
import hashlib
import base64
import asyncio
import sys
import threading
import traceback
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

from aiohttp import web
from PIL import Image, ImageOps

from ..config import settings
from ..logger import log_action
from ..vision import vision as V
from ..services.catsheets import get_photo_metadata_rows, force_refresh_photo_rows_cache
from ..services import labeler_cache, local_photos
from ..services.gallery_retrain import get_gallery_retrain_status, schedule_gallery_retrain

_LABELER_ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv("UI_ALLOWED_ORIGINS", "http://localhost:8080").split(",")
    if origin.strip()
}
_SAVE_BATCH_MAX = max(1, int(os.getenv("LABELER_SAVE_BATCH_MAX", "500") or "500"))

#Column indices in local photo metadata rows (0-indexed)
COL_CAT_ID = 0       #A: CatID (e.g., "1. Twix")
COL_URL = 6          #G: Picture Link
COL_SERIAL = 7       #H: Serial number
COL_BOX_COORDS = 8   #I: BoxCoordinates
COL_BOX_CAT_IDS = 9  #J: BoxCatIDs

#Regex for serial extraction
SN_PATTERN = re.compile(r"sn(\d+)", re.IGNORECASE)
_IDENTIFY_CONCURRENCY = max(1, int(os.getenv("LABELER_IDENTIFY_CONCURRENCY", "2") or "2"))
_IDENTIFY_TIMEOUT_SEC = float(os.getenv("LABELER_IDENTIFY_TIMEOUT_SEC", "45") or "45")
_IDENTIFY_PREFETCH_TIMEOUT_SEC = float(os.getenv("LABELER_IDENTIFY_PREFETCH_TIMEOUT_SEC", "20") or "20")
_IDENTIFY_PREFETCH_MAX_BOXES = max(1, int(os.getenv("LABELER_IDENTIFY_PREFETCH_MAX_BOXES", "12") or "12"))
_IDENTIFY_PREFETCH_REFS_PER_CANDIDATE = max(
    1,
    int(os.getenv("LABELER_IDENTIFY_PREFETCH_REFS_PER_CANDIDATE", "5") or "5"),
)
_MANUAL_CONCURRENCY = max(1, int(os.getenv("LABELER_MANUAL_CONCURRENCY", "1") or "1"))
_MANUAL_TIMEOUT_SEC = float(os.getenv("LABELER_MANUAL_TIMEOUT_SEC", "60") or "60")
_DETECT_CONCURRENCY = max(1, int(os.getenv("LABELER_DETECT_CONCURRENCY", "2") or "2"))
_REFINE_CONCURRENCY = max(1, int(os.getenv("LABELER_REFINE_CONCURRENCY", "2") or "2"))
_DETECT_TIMEOUT_SEC = float(os.getenv("LABELER_DETECT_TIMEOUT_SEC", "25") or "25")
_DETECT_PREFETCH_TIMEOUT_SEC = float(os.getenv("LABELER_DETECT_PREFETCH_TIMEOUT_SEC", "45") or "45")
_REFINE_TIMEOUT_SEC = float(os.getenv("LABELER_REFINE_TIMEOUT_SEC", "20") or "20")
_REFINE_PREFETCH_TIMEOUT_SEC = float(os.getenv("LABELER_REFINE_PREFETCH_TIMEOUT_SEC", "20") or "20")
_DETECT_INLINE_SAM_TIMEOUT_SEC = float(
    os.getenv("LABELER_DETECT_INLINE_SAM_TIMEOUT_SEC", "20") or "20"
)
_DETECT_INLINE_SAM_PASSES = max(1, int(os.getenv("LABELER_DETECT_INLINE_SAM_PASSES", "1") or "1"))
_DETECT_PIPELINE_VERSION = "guarded_sam_v4_strict_queue"
_REFINE_PIPELINE_VERSION = "guarded_sam_v4_strict_queue"
_identify_sem = asyncio.Semaphore(_IDENTIFY_CONCURRENCY)
_manual_sem = asyncio.Semaphore(_MANUAL_CONCURRENCY)
_detect_sem = asyncio.Semaphore(_DETECT_CONCURRENCY)
_refine_sem = asyncio.Semaphore(_REFINE_CONCURRENCY)
_HEAVY_CONCURRENCY = max(1, int(os.getenv("LABELER_HEAVY_CONCURRENCY", "3") or "3"))
_heavy_sem = asyncio.Semaphore(_HEAVY_CONCURRENCY)
_DEFAULT_REF_CROP_RENDER_CONCURRENCY = max(4, min(16, int(os.cpu_count() or 8)))
_REF_CROP_RENDER_CONCURRENCY = max(
    1,
    int(
        os.getenv(
            "LABELER_REF_CROP_RENDER_CONCURRENCY",
            str(_DEFAULT_REF_CROP_RENDER_CONCURRENCY),
        )
        or str(_DEFAULT_REF_CROP_RENDER_CONCURRENCY)
    ),
)
_ref_crop_render_sem = asyncio.Semaphore(_REF_CROP_RENDER_CONCURRENCY)
_identify_prefetch_timeout_streak = 0
_identify_prefetch_backoff_until_mono = 0.0

#Event loop health monitoring
_loop_lag_ms: float = 0.0
_loop_lag_max_ms: float = 0.0
_loop_lag_monitor_started = False
_LOOP_LAG_INTERVAL_SEC = 2.0
_LOOP_LAG_LOG_THRESHOLD_MS = 500.0
_LOOP_LAG_SHED_BG_WORK_MS = max(
    300.0,
    float(os.getenv("LABELER_LOOP_LAG_SHED_BG_WORK_MS", "900") or "900"),
)
_LOOP_LAG_LOG_COOLDOWN_SEC = max(
    1.0,
    float(os.getenv("LABELER_LOOP_LAG_LOG_COOLDOWN_SEC", "20") or "20"),
)
_LOOP_LAG_LOG_MIN_DELTA_MS = max(
    100.0,
    float(os.getenv("LABELER_LOOP_LAG_LOG_MIN_DELTA_MS", "1200") or "1200"),
)
_loop_lag_last_log_mono: float = 0.0
_loop_lag_last_logged_ms: float = 0.0
_loop_heartbeat_mono: float = time.monotonic()
_loop_watchdog_started = False
_LOOP_HEARTBEAT_INTERVAL_SEC = max(
    0.05,
    float(os.getenv("LABELER_LOOP_HEARTBEAT_INTERVAL_SEC", "0.25") or "0.25"),
)
_LOOP_STALL_STACK_THRESHOLD_MS = max(
    1500.0,
    float(os.getenv("LABELER_LOOP_STALL_STACK_THRESHOLD_MS", "2500") or "2500"),
)
_LOOP_STALL_STACK_POLL_SEC = max(
    0.1,
    float(os.getenv("LABELER_LOOP_STALL_STACK_POLL_SEC", "0.5") or "0.5"),
)
_LOOP_STALL_STACK_MAX_FRAMES = max(
    4,
    min(20, int(os.getenv("LABELER_LOOP_STALL_STACK_MAX_FRAMES", "12") or "12")),
)
_loop_stall_last_logged_heartbeat_mono: float = 0.0
_main_thread_ident = threading.main_thread().ident


async def _event_loop_heartbeat() -> None:
    """Publish a frequent heartbeat so a watchdog thread can detect live stalls."""
    global _loop_heartbeat_mono
    while True:
        _loop_heartbeat_mono = time.monotonic()
        await asyncio.sleep(_LOOP_HEARTBEAT_INTERVAL_SEC)


def _snapshot_main_thread_stack(limit: int = 12) -> Dict[str, Any]:
    """Capture the current Python stack for the main thread."""
    try:
        ident = _main_thread_ident
        frame = sys._current_frames().get(ident) if ident is not None else None
        if frame is None:
            return {"available": False}
        stack = traceback.extract_stack(frame)
        tail = stack[-max(1, int(limit)):]
        return {
            "available": True,
            "thread": threading.main_thread().name,
            "frames": [
                {
                    "file": str(entry.filename or ""),
                    "line": int(entry.lineno or 0),
                    "func": str(entry.name or ""),
                    "code": str((entry.line or "").strip()),
                }
                for entry in tail
            ],
        }
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e!r}"}


def _event_loop_stall_watchdog() -> None:
    """Log the main-thread stack once per stall while the loop is still wedged."""
    global _loop_stall_last_logged_heartbeat_mono
    while True:
        try:
            time.sleep(_LOOP_STALL_STACK_POLL_SEC)
            heartbeat_mono = float(_loop_heartbeat_mono)
            age_ms = max(0.0, (time.monotonic() - heartbeat_mono) * 1000.0)
            if age_ms < float(_LOOP_STALL_STACK_THRESHOLD_MS):
                continue
            if heartbeat_mono <= float(_loop_stall_last_logged_heartbeat_mono):
                continue
            payload = {
                "heartbeat_age_ms": int(round(age_ms)),
                "threshold_ms": int(round(float(_LOOP_STALL_STACK_THRESHOLD_MS))),
                "last_known_loop_lag_ms": int(round(float(_loop_lag_ms))),
                "last_known_loop_lag_max_ms": int(round(float(_loop_lag_max_ms))),
                "main_thread_stack": _snapshot_main_thread_stack(_LOOP_STALL_STACK_MAX_FRAMES),
            }
            log_action(
                "labeler_loop_stall_stack",
                f"age_ms={int(round(age_ms))}; threshold_ms={int(round(float(_LOOP_STALL_STACK_THRESHOLD_MS)))}",
                json.dumps(payload, separators=(",", ":")),
            )
            _loop_stall_last_logged_heartbeat_mono = heartbeat_mono
        except Exception as e:
            log_action("labeler_loop_stall_watchdog_error", "error", f"{type(e).__name__}: {e!r}")
            time.sleep(1.0)


async def _event_loop_lag_monitor() -> None:
    """Continuously measure event loop responsiveness.

    Sleeps for _LOOP_LAG_INTERVAL_SEC and measures how late the wakeup is.
    High lag means the event loop is saturated with other work.
    """
    global _loop_lag_ms, _loop_lag_max_ms
    global _loop_lag_last_log_mono, _loop_lag_last_logged_ms
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(_LOOP_LAG_INTERVAL_SEC)
        elapsed = (time.perf_counter() - t0) * 1000.0
        expected = _LOOP_LAG_INTERVAL_SEC * 1000.0
        lag = max(0.0, elapsed - expected)
        _loop_lag_ms = lag
        _loop_lag_max_ms = max(_loop_lag_max_ms, lag)
        if lag >= _LOOP_LAG_LOG_THRESHOLD_MS:
            now_mono = time.monotonic()
            cooldown_ok = (now_mono - float(_loop_lag_last_log_mono)) >= float(_LOOP_LAG_LOG_COOLDOWN_SEC)
            delta_ok = abs(float(lag) - float(_loop_lag_last_logged_ms)) >= float(_LOOP_LAG_LOG_MIN_DELTA_MS)
            if cooldown_ok or delta_ok:
                runtime_snapshot = _labeler_runtime_snapshot()
                log_action(
                    "labeler_event_loop_lag",
                    f"lag_ms={int(lag)}; max_ms={int(_loop_lag_max_ms)}",
                    json.dumps(runtime_snapshot, separators=(",", ":")),
                )
                _loop_lag_last_log_mono = now_mono
                _loop_lag_last_logged_ms = float(lag)


def _ensure_loop_lag_monitor() -> None:
    """Start the event loop lag monitor if not already running."""
    global _loop_lag_monitor_started
    global _loop_watchdog_started
    if _loop_lag_monitor_started:
        return
    _loop_lag_monitor_started = True
    try:
        asyncio.create_task(_event_loop_heartbeat())
        asyncio.create_task(_event_loop_lag_monitor())
        if not _loop_watchdog_started:
            watchdog = threading.Thread(
                target=_event_loop_stall_watchdog,
                name="labeler_loop_watchdog",
                daemon=True,
            )
            watchdog.start()
            _loop_watchdog_started = True
    except Exception:
        _loop_lag_monitor_started = False
        _loop_watchdog_started = False


def _get_task_census() -> str:
    """Return a summary of active asyncio tasks by coroutine name."""
    try:
        snap = _get_task_census_snapshot()
        top = list(snap.get("top") or [])
        parts = [f"{str(row.get('name') or 'unknown')}={int(row.get('count') or 0)}" for row in top]
        return f"total={int(snap.get('total') or 0)}; " + "; ".join(parts)
    except Exception:
        return "error"


def _get_task_census_snapshot(limit: int = 8) -> Dict[str, Any]:
    all_tasks = asyncio.all_tasks()
    counts: Dict[str, int] = {}
    for t in all_tasks:
        coro = t.get_coro()
        name = getattr(coro, "__qualname__", None) or getattr(coro, "__name__", None) or "unknown"
        if "." in name:
            name = name.rsplit(".", 1)[-1]
        counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:max(1, int(limit))]
    return {
        "total": int(len(all_tasks)),
        "top": [{"name": str(name), "count": int(count)} for name, count in top],
    }


def _thread_group_name(name: str) -> str:
    raw = str(name or "unknown").strip() or "unknown"
    raw = re.sub(r"[_-]?\d+$", "", raw)
    return raw or "unknown"


def _get_thread_census_snapshot(limit: int = 8) -> Dict[str, Any]:
    try:
        counts: Dict[str, int] = {}
        threads = list(threading.enumerate())
        for th in threads:
            name = _thread_group_name(getattr(th, "name", "unknown"))
            counts[name] = counts.get(name, 0) + 1
        top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:max(1, int(limit))]
        return {
            "total": int(len(threads)),
            "top": [{"name": str(name), "count": int(count)} for name, count in top],
        }
    except Exception:
        return {"total": 0, "top": [], "error": True}


def _safe_sem_waiter_count(sem: asyncio.Semaphore) -> int:
    try:
        waiters = getattr(sem, "_waiters", None)
        if waiters is None:
            return 0
        return int(len(waiters))
    except Exception:
        return 0


def _sem_snapshot(sem: asyncio.Semaphore, limit: int) -> Dict[str, int]:
    available = 0
    try:
        available = int(getattr(sem, "_value", 0) or 0)
    except Exception:
        available = 0
    limit_i = max(0, int(limit))
    return {
        "limit": limit_i,
        "available": max(0, available),
        "in_use": max(0, limit_i - available),
        "waiters": max(0, _safe_sem_waiter_count(sem)),
    }


def _claim_snapshot() -> Dict[str, Any]:
    per_mode: Dict[str, int] = {}
    for (mode, _serial), _payload in list(_active_claims.items()):
        key = str(mode or "unknown")
        per_mode[key] = per_mode.get(key, 0) + 1
    return {
        "active": int(len(_active_claims)),
        "by_mode": per_mode,
    }


def _labeler_runtime_snapshot() -> Dict[str, Any]:
    return {
        "tasks": _get_task_census_snapshot(),
        "threads": _get_thread_census_snapshot(),
        "semaphores": {
            "detect": _sem_snapshot(_detect_sem, _DETECT_CONCURRENCY),
            "refine": _sem_snapshot(_refine_sem, _REFINE_CONCURRENCY),
            "identify": _sem_snapshot(_identify_sem, _IDENTIFY_CONCURRENCY),
            "manual": _sem_snapshot(_manual_sem, _MANUAL_CONCURRENCY),
            "heavy": _sem_snapshot(_heavy_sem, _HEAVY_CONCURRENCY),
            "ref_crop_render": _sem_snapshot(_ref_crop_render_sem, _REF_CROP_RENDER_CONCURRENCY),
            "ref_crop_warm_render": _sem_snapshot(_ref_crop_warm_render_sem, _REF_CROP_WARM_CONCURRENCY),
        },
        "caches": {
            "detect": int(len(_detect_result_cache)),
            "refine": int(len(_refine_result_cache)),
            "identify": int(len(_identify_result_cache)),
            "manual": int(len(_manual_result_cache)),
            "quality": int(len(_classify_quality_cache)),
            "quality_soft_fail": int(len(_classify_quality_soft_fail_cache)),
        },
        "claims": _claim_snapshot(),
        "inflight": {
            "identify_singleflight": int(len(_identify_inflight)),
            "quality_eval": int(len(_classify_quality_inflight)),
            "auto_reject_quality": int(len(_auto_reject_quality_inflight)),
            "quality_scan": int(len(_classify_quality_scan_inflight)),
            "detector_warm_task": int(bool(_detector_warm_task and not _detector_warm_task.done())),
        },
        "downloads": _labeler_download_stats(),
        "loop_lag_ms": int(round(float(_loop_lag_ms))),
        "loop_lag_max_ms": int(round(float(_loop_lag_max_ms))),
        "warm_generation": int(_warm_generation),
        "warm_generation_grace": int(_WARM_GENERATION_GRACE),
    }
_CV_RESULT_TTL_SEC = max(5.0, float(os.getenv("LABELER_CV_RESULT_TTL_SEC", "180") or "180"))
_DETECT_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_DETECT_RESULT_CACHE_MAX", "1000") or "1000"))
_REFINE_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_REFINE_RESULT_CACHE_MAX", "1200") or "1200"))
_IDENTIFY_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_IDENTIFY_RESULT_CACHE_MAX", "900") or "900"))
_MANUAL_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_MANUAL_RESULT_CACHE_MAX", "600") or "600"))
_REF_CROP_RESULT_TTL_SEC = max(
    _CV_RESULT_TTL_SEC,
    float(os.getenv("LABELER_REF_CROP_RESULT_TTL_SEC", "21600") or "21600"),
)
_REF_CROP_WARM_SIZE = max(
    96,
    min(512, int(os.getenv("LABELER_REF_CROP_WARM_SIZE", "480") or "480")),
)
_REF_CROP_WARM_CONCURRENCY = max(
    1,
    int(
        os.getenv(
            "LABELER_REF_CROP_WARM_CONCURRENCY",
            str(_DEFAULT_REF_CROP_RENDER_CONCURRENCY),
        )
        or str(_DEFAULT_REF_CROP_RENDER_CONCURRENCY)
    ),
)
# Separate semaphore exclusively for background warm tasks. On-demand requests from
# the get_ref_crop endpoint use _ref_crop_render_sem (higher concurrency) and are
# completely isolated from warm tasks, so warm cannot starve foreground renders.
_ref_crop_warm_render_sem = asyncio.Semaphore(_REF_CROP_WARM_CONCURRENCY)
# Generation counter for warm work.  Each new scheduling call increments the
# generation.  Tasks whose generation falls more than _WARM_GENERATION_GRACE
# behind the current value bail out before doing expensive work, so the most
# recent item always gets priority and old queue entries drain quickly.
_warm_generation: int = 0
_WARM_GENERATION_GRACE = max(
    1,
    int(os.getenv("LABELER_WARM_GENERATION_GRACE", "3") or "3"),
)
_CLASSIFY_REF_CROP_WARM_MAX_CANDIDATES = max(
    1,
    int(os.getenv("LABELER_CLASSIFY_REF_CROP_WARM_MAX_CANDIDATES", "9") or "9"),
)
_CLASSIFY_REF_CROP_WARM_MAX_REFS = max(
    1,
    int(os.getenv("LABELER_CLASSIFY_REF_CROP_WARM_MAX_REFS", "5") or "5"),
)
_MANUAL_REF_CROP_WARM_MAX_CANDIDATES = max(
    1,
    int(os.getenv("LABELER_MANUAL_REF_CROP_WARM_MAX_CANDIDATES", "18") or "18"),
)
_MANUAL_REF_CROP_WARM_MAX_REFS = max(
    1,
    int(os.getenv("LABELER_MANUAL_REF_CROP_WARM_MAX_REFS", "3") or "3"),
)
_CLASSIFY_MIN_PIXELS = max(0, int(os.getenv("LABELER_CLASSIFY_MIN_PIXELS", "122500") or "122500"))
_CLASSIFY_MIN_DIM = max(0, int(os.getenv("LABELER_CLASSIFY_MIN_DIM", "0") or "0"))
_CLASSIFY_MIN_BLUR = max(0.0, float(os.getenv("LABELER_CLASSIFY_MIN_BLUR", "35") or "35"))
_CLASSIFY_BLUR_MAX_DIM = max(64, int(os.getenv("LABELER_CLASSIFY_BLUR_MAX_DIM", "640") or "640"))
_CLASSIFY_PREFILTER_MAX_SYNC_ITEMS = max(
    0,
    int(os.getenv("LABELER_CLASSIFY_PREFILTER_MAX_SYNC_ITEMS", "4") or "4"),
)
_CLASSIFY_PREFILTER_SYNC_ITEM_TIMEOUT_SEC = max(
    0.5,
    float(os.getenv("LABELER_CLASSIFY_PREFILTER_SYNC_ITEM_TIMEOUT_SEC", "3") or "3"),
)
_CLASSIFY_PREFILTER_BG_CONCURRENCY = max(
    1,
    min(10, int(os.getenv("LABELER_CLASSIFY_PREFILTER_BG_CONCURRENCY", "2") or "2")),
)
_CLASSIFY_PREFILTER_BG_QUEUE_SCAN_MAX_ITEMS = max(
    0,
    int(os.getenv("LABELER_CLASSIFY_PREFILTER_BG_QUEUE_SCAN_MAX_ITEMS", "12") or "12"),
)
_CLASSIFY_PREFILTER_BG_QUEUE_SCAN_COOLDOWN_SEC = max(
    0.0,
    float(os.getenv("LABELER_CLASSIFY_PREFILTER_BG_QUEUE_SCAN_COOLDOWN_SEC", "12") or "12"),
)
_CLASSIFY_PREFILTER_CACHE_TTL_SEC = max(
    30.0,
    float(os.getenv("LABELER_CLASSIFY_PREFILTER_CACHE_TTL_SEC", "1800") or "1800"),
)
_CLASSIFY_PREFILTER_SOFT_FAIL_TTL_SEC = max(
    5.0,
    min(
        float(_CLASSIFY_PREFILTER_CACHE_TTL_SEC),
        float(os.getenv("LABELER_CLASSIFY_PREFILTER_SOFT_FAIL_TTL_SEC", "120") or "120"),
    ),
)
_CLASSIFY_PREFILTER_CACHE_MAX = max(
    500,
    int(os.getenv("LABELER_CLASSIFY_PREFILTER_CACHE_MAX", "20000") or "20000"),
)
_IDENTIFY_ALLOW_MANUAL_REF_FALLBACK = str(
    os.getenv("LABELER_IDENTIFY_ALLOW_MANUAL_REF_FALLBACK", "1")
).strip().lower() in {"1", "true", "yes", "on"}
_IDENTIFY_DEBUG = str(
    os.getenv("LABELER_IDENTIFY_DEBUG", "0")
).strip().lower() in {"1", "true", "yes", "on"}
_IDENTIFY_DEBUG_PREFETCH_SAMPLE = max(
    0.0,
    min(1.0, float(os.getenv("LABELER_IDENTIFY_DEBUG_PREFETCH_SAMPLE", "0.2") or "0.2")),
)
_IDENTIFY_REF_PIPELINE_VERSION = "dino_refs_v4"
_LABELER_CLAIM_TTL_SEC = max(30, int(os.getenv("LABELER_CLAIM_TTL_SEC", "180") or "180"))
_claim_lock = asyncio.Lock()
_active_claims: Dict[Tuple[str, int], Dict[str, Any]] = {}
_detector_warm_task: Optional[asyncio.Task] = None
_detector_warm_done: bool = False
_detect_result_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_refine_result_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_identify_result_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_manual_result_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_identify_inflight_lock = asyncio.Lock()
_identify_inflight: Dict[str, asyncio.Future] = {}
_classify_quality_inflight_lock = asyncio.Lock()
_classify_quality_inflight: Dict[int, asyncio.Future] = {}
_classify_quality_cache: Dict[int, Tuple[float, bool, int, int, float]] = {}
_classify_quality_soft_fail_cache: Dict[int, Tuple[float, str]] = {}
_auto_reject_quality_inflight: set[int] = set()
_classify_quality_scan_inflight: set[int] = set()
_classify_quality_bg_queue_scan_next_mono: float = 0.0
_CAT_ID_NAME_RE = re.compile(r"^\s*(\d+)\s*[.)\-:]?\s*(.+?)\s*$")
_NEEDS_REVIEW_LABELS = {"needsreview", "needs review"}
_SKIP_CATID_LABELS = {
    "",
    "rejected",
    "needsreview",
    "needs review",
    "notacat",
    "not a cat",
    "0. notacat",
    "0.notacat",
}
_MANUAL_FALLBACK_REFS_PER_CAT = max(1, int(os.getenv("LABELER_MANUAL_FALLBACK_REFS_PER_CAT", "5") or "5"))
_MANUAL_QUERY_REFS_PER_CAT = max(
    0,
    int(os.getenv("LABELER_MANUAL_QUERY_REFS_PER_CAT", "1") or "1"),
)
_MANUAL_QUERY_REF_CAT_LIMIT = max(
    0,
    int(os.getenv("LABELER_MANUAL_QUERY_REF_CAT_LIMIT", "24") or "24"),
)
_MANUAL_QUERY_REF_SEARCH_POOL = max(
    max(1, int(_MANUAL_QUERY_REFS_PER_CAT or 0)),
    int(os.getenv("LABELER_MANUAL_QUERY_REF_SEARCH_POOL", "12") or "12"),
)
_MANUAL_CANDIDATE_PIPELINE_VERSION = "manual_rank_v2_local_refs"
_MANUAL_METADATA_REF_SAMPLE_PER_CAT = max(
    _MANUAL_FALLBACK_REFS_PER_CAT,
    int(
        os.getenv(
            "LABELER_MANUAL_METADATA_REF_SAMPLE_PER_CAT",
            "20",
        ) or "20"
    ),
)
_MANUAL_METADATA_REF_CROPPED_SAMPLE_PER_CAT = max(
    _MANUAL_METADATA_REF_SAMPLE_PER_CAT,
    int(
        os.getenv(
            "LABELER_MANUAL_METADATA_REF_CROPPED_SAMPLE_PER_CAT",
            "40",
        ) or "40"
    ),
)
_MANUAL_METADATA_REF_UNCROPPED_SAMPLE_PER_CAT = max(
    1,
    int(
        os.getenv(
            "LABELER_MANUAL_METADATA_REF_UNCROPPED_SAMPLE_PER_CAT",
            "8",
        ) or "8"
    ),
)
_MANUAL_METADATA_REF_TTL_SEC = max(
    30,
    int(
        os.getenv(
            "LABELER_MANUAL_METADATA_REF_TTL_SEC",
            "600",
        ) or "600"
    ),
)
_manual_metadata_ref_lock = asyncio.Lock()
_manual_metadata_ref_cache: Dict[str, List[Dict[str, Any]]] = {}
_manual_metadata_ref_built_mono: float = 0.0
_photo_crop_index_lock = asyncio.Lock()
_photo_crop_index_cache: Dict[Tuple[int, int], Dict[str, Any]] = {}
_photo_crop_index_built_mono: float = 0.0
#Bumped once per completed rebuild. Callers that queued behind the lock compare
#against the value they captured on the way in, which is exact; the previous
#check compared time.monotonic() readings, so on a coarse clock (Windows ticks
#at ~15ms) a rebuild that landed inside the same tick as the request did not
#count as newer and every waiter rebuilt anyway.
_photo_crop_index_generation: int = 0
#Floor on how often a cache miss may force a full crop-index rebuild.
_PHOTO_CROP_INDEX_MISS_REBUILD_INTERVAL_SEC = max(
    1.0,
    float(os.getenv("LABELER_PHOTO_CROP_INDEX_MISS_REBUILD_INTERVAL_SEC", "15") or "15"),
)
_PHOTO_CROP_INDEX_FORCE_COALESCE_SEC = max(
    0.01,
    float(os.getenv("LABELER_PHOTO_CROP_INDEX_FORCE_COALESCE_SEC", "0.25") or "0.25"),
)
_photo_crop_index_miss_rebuild_next_mono: float = 0.0
_ref_crop_result_cache: Dict[str, Tuple[float, bytes]] = {}
#serial -> rendered ref-crop cache keys, so a save can drop just the crops it
#touched instead of throwing away every rendered thumbnail in the process.
_ref_crop_cache_keys_by_serial: Dict[int, Set[str]] = {}
_ref_crop_negative_cache: Dict[Tuple[int, int], float] = {}
_REF_CROP_RESULT_CACHE_MAX = max(200, int(os.getenv("LABELER_REF_CROP_RESULT_CACHE_MAX", "3000") or "3000"))
_REF_CROP_SERIAL_INDEX_MAX = max(500, int(_REF_CROP_RESULT_CACHE_MAX))
_REF_CROP_MISS_LOG_COOLDOWN_SEC = max(
    5.0,
    float(os.getenv("LABELER_REF_CROP_MISS_LOG_COOLDOWN_SEC", "90") or "90"),
)
_REF_CROP_IMAGE_UNAVAILABLE_WINDOW_SEC = max(
    10.0,
    float(os.getenv("LABELER_REF_CROP_IMAGE_UNAVAILABLE_WINDOW_SEC", "30") or "30"),
)
_REF_CROP_IMAGE_UNAVAILABLE_MAX_PER_WINDOW = max(
    1,
    int(os.getenv("LABELER_REF_CROP_IMAGE_UNAVAILABLE_MAX_PER_WINDOW", "24") or "24"),
)
_LOG_REF_CROP_IMAGE_UNAVAILABLE_MISS = str(
    os.getenv("LABELER_LOG_REF_CROP_IMAGE_UNAVAILABLE_MISS", "0")
).strip().lower() in {"1", "true", "yes", "on"}
_ref_crop_miss_next_log_mono: Dict[str, float] = {}
_ref_crop_miss_suppressed: Dict[str, int] = {}
_ref_crop_img_unavail_window_start_mono: float = 0.0
_ref_crop_img_unavail_logged: int = 0
_ref_crop_img_unavail_suppressed: int = 0
_flagged_ref_serials: Set[int] = set()
_flagged_ref_serials_loaded: bool = False
_FLAGGED_REF_SERIALS_PATH = Path("cache") / "labeler" / "flagged_ref_serials.json"
_flag_incorrect_queue_lock = asyncio.Lock()
_flag_incorrect_queue_loaded: bool = False
_flag_incorrect_queue: Dict[int, Dict[str, Any]] = {}
_flag_incorrect_worker_task: Optional[asyncio.Task] = None
_FLAG_INCORRECT_QUEUE_PATH = Path("cache") / "labeler" / "flag_incorrect_queue.json"
_FLAG_INCORRECT_QUEUE_BATCH_MAX = max(
    1,
    int(os.getenv("LABELER_FLAG_INCORRECT_QUEUE_BATCH_MAX", "24") or "24"),
)
_FLAG_INCORRECT_QUEUE_RETRY_DELAY_SEC = max(
    1.0,
    float(os.getenv("LABELER_FLAG_INCORRECT_QUEUE_RETRY_DELAY_SEC", "8") or "8"),
)
_PROFILE_REFRESH_MIN_SEC = max(60, int(os.getenv("LABELER_PROFILE_REFRESH_MIN_SEC", "300") or "300"))
_profile_refresh_mono: float = 0.0
_UI_DIAG_VERBOSE = str(
    os.getenv("LABELER_UI_DIAG_VERBOSE", "0")
).strip().lower() in {"1", "true", "yes", "on"}
_QUEUE_ROWS_TTL_SEC = max(
    30,
    int(os.getenv("LABELER_QUEUE_ROWS_TTL_SEC", "60") or "60"),
)
_QUEUE_ROWS_TTL_LOCAL_ONLY_SEC = max(
    _QUEUE_ROWS_TTL_SEC,
    int(os.getenv("LABELER_QUEUE_ROWS_TTL_LOCAL_ONLY_SEC", "300") or "300"),
)
_QUEUE_CLASSIFY_SLOW_LOG_THRESHOLD_MS = max(
    250.0,
    float(os.getenv("LABELER_QUEUE_CLASSIFY_SLOW_LOG_THRESHOLD_MS", "1800") or "1800"),
)
_QUEUE_CLASSIFY_SLOW_LOG_COOLDOWN_SEC = max(
    2.0,
    float(os.getenv("LABELER_QUEUE_CLASSIFY_SLOW_LOG_COOLDOWN_SEC", "30") or "30"),
)
_queue_classify_slow_last_log_mono: float = 0.0
_CLAIM_DIAG_SLOW_MS = max(
    120,
    int(os.getenv("LABELER_CLAIM_DIAG_SLOW_MS", "800") or "800"),
)
_CLAIM_DIAG_SAMPLE = max(
    0.0,
    min(1.0, float(os.getenv("LABELER_CLAIM_DIAG_SAMPLE", "0.08") or "0.08")),
)
_PHOTO_METADATA_CACHE_REFRESH_COOLDOWN_SEC = max(
    2.0,
    float(
        os.getenv(
            "PHOTO_METADATA_CACHE_REFRESH_COOLDOWN_SEC",
            "20",
        ) or "20"
    ),
)
_photo_metadata_cache_refresh_task: Optional[asyncio.Task] = None
_photo_metadata_cache_refresh_next_allowed_mono: float = 0.0
_QUEUE_CACHE_WARM_COOLDOWN_SEC = max(
    1.0,
    float(os.getenv("LABELER_QUEUE_CACHE_WARM_COOLDOWN_SEC", "8") or "8"),
)
_QUEUE_CACHE_WARM_TARGET = max(
    10,
    int(os.getenv("LABELER_QUEUE_WARM_TARGET", "360") or "360"),
)
_QUEUE_CACHE_WARM_SCAN_LIMIT = max(
    _QUEUE_CACHE_WARM_TARGET,
    int(os.getenv("LABELER_QUEUE_WARM_SCAN_LIMIT", "600") or "600"),
)
_BATCH_WARM_ENABLE = str(
    os.getenv("LABELER_BATCH_WARM_ENABLE", "1")
).strip().lower() in {"1", "true", "yes", "on"}
_BOOT_WARM_ENABLE = str(
    os.getenv("LABELER_BOOT_WARM_ENABLE", "1")
).strip().lower() in {"1", "true", "yes", "on"}
_BOOT_WARM_BUDGET_GB = max(
    0.5,
    float(os.getenv("LABELER_BOOT_WARM_BUDGET_GB", "12") or "12"),
)
_BOOT_WARM_SCAN_LIMIT = max(
    100,
    int(os.getenv("LABELER_BOOT_WARM_SCAN_LIMIT", "1500") or "1500"),
)
_BOOT_WARM_CONCURRENCY = max(
    1,
    int(os.getenv("LABELER_BOOT_WARM_CONCURRENCY", "4") or "4"),
)
_queue_cache_warm_next_mono: Dict[str, float] = {"detect": 0.0, "classify": 0.0, "manual": 0.0}
_boot_cache_warm_task: Optional[asyncio.Task] = None
_boot_cache_warm_started: bool = False
_LOCAL_MISSING_SAMPLE_MAX = 50
_local_mode_logged: bool = False
_PHOTO_ITEM_CONTEXT_CACHE_TTL_SEC = max(
    30.0,
    float(os.getenv("LABELER_PHOTO_ITEM_CONTEXT_CACHE_TTL_SEC", "300") or "300"),
)
_DISCORD_CONTEXT_CACHE_TTL_SEC = max(
    300.0,
    float(os.getenv("LABELER_DISCORD_CONTEXT_CACHE_TTL_SEC", "43200") or "43200"),
)
_CONTEXT_WARM_CONCURRENCY = max(
    1,
    min(12, int(os.getenv("LABELER_CONTEXT_WARM_CONCURRENCY", "6") or "6")),
)
_CONTEXT_CACHE_MAX_ITEMS = max(
    256,
    int(os.getenv("LABELER_CONTEXT_CACHE_MAX_ITEMS", "6000") or "6000"),
)
_QUEUE_LOCAL_FILTER_LOG_COOLDOWN_SEC = max(
    5.0,
    float(os.getenv("LABELER_QUEUE_LOCAL_FILTER_LOG_COOLDOWN_SEC", "60") or "60"),
)
_queue_local_filter_next_log_mono: Dict[str, float] = {}
_photo_item_context_cache: Dict[int, Dict[str, Any]] = {}
_photo_item_context_cache_built_mono: float = 0.0
_photo_item_context_cache_lock = asyncio.Lock()
_discord_member_display_cache: Dict[Tuple[int, int], Tuple[float, str]] = {}
_discord_user_display_cache: Dict[int, Tuple[float, str]] = {}
_discord_channel_name_cache: Dict[int, Tuple[float, str]] = {}
#Cache key -> monotonic expiry for lookups Discord could not resolve at all.
_discord_context_unresolved: Dict[Any, float] = {}
_DISCORD_CONTEXT_NEGATIVE_TTL_SEC = max(
    60.0,
    float(os.getenv("LABELER_DISCORD_CONTEXT_NEGATIVE_TTL_SEC", "900") or "900"),
)


def _parse_serial(val: str) -> Optional[int]:
    """Parse serial from string like 'sn1234' or just '1234'."""
    sval = str(val or "").strip()
    m = SN_PATTERN.search(sval)
    if m:
        return int(m.group(1))
    if sval.isdigit():
        return int(sval)
    return None


def _ensure_flagged_ref_serials_loaded() -> None:
    global _flagged_ref_serials_loaded, _flagged_ref_serials
    if _flagged_ref_serials_loaded:
        return
    try:
        if _FLAGGED_REF_SERIALS_PATH.exists():
            raw = json.loads(_FLAGGED_REF_SERIALS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                vals: Set[int] = set()
                for item in raw:
                    try:
                        sn = int(item)
                    except Exception:
                        continue
                    if sn > 0:
                        vals.add(sn)
                _flagged_ref_serials = vals
    except Exception as e:
        log_action("labeler_flagged_refs_load_error", "error", f"{type(e).__name__}: {e!r}")
    _flagged_ref_serials_loaded = True


def _persist_flagged_ref_serials() -> None:
    try:
        _FLAGGED_REF_SERIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FLAGGED_REF_SERIALS_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(sorted(int(sn) for sn in _flagged_ref_serials if int(sn) > 0), separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(_FLAGGED_REF_SERIALS_PATH)
    except Exception as e:
        log_action("labeler_flagged_refs_save_error", "error", f"{type(e).__name__}: {e!r}")


def _is_flagged_ref_serial(serial: Any) -> bool:
    try:
        sn = int(serial)
    except Exception:
        return False
    if sn <= 0:
        return False
    _ensure_flagged_ref_serials_loaded()
    return int(sn) in _flagged_ref_serials


def _add_flagged_ref_serial(serial: Any) -> bool:
    try:
        sn = int(serial)
    except Exception:
        return False
    if sn <= 0:
        return False
    _ensure_flagged_ref_serials_loaded()
    if sn in _flagged_ref_serials:
        return False
    _flagged_ref_serials.add(sn)
    _persist_flagged_ref_serials()
    return True


def _discard_flagged_ref_serial(serial: Any) -> bool:
    try:
        sn = int(serial)
    except Exception:
        return False
    if sn <= 0:
        return False
    _ensure_flagged_ref_serials_loaded()
    if sn not in _flagged_ref_serials:
        return False
    _flagged_ref_serials.discard(sn)
    _persist_flagged_ref_serials()
    return True


def _invalidate_labeler_caches_after_label_clears(serials: Optional[List[int]] = None) -> None:
    """Invalidate in-memory labeler caches after photo metadata labels change."""
    global _manual_metadata_ref_cache, _manual_metadata_ref_built_mono
    global _photo_crop_index_cache, _photo_crop_index_built_mono
    for item in serials or []:
        try:
            sn = int(item)
        except Exception:
            continue
        if sn <= 0:
            continue
        _classify_quality_cache.pop(int(sn), None)
        _classify_quality_soft_fail_cache.pop(int(sn), None)
        _auto_reject_quality_inflight.discard(int(sn))
        _classify_quality_scan_inflight.discard(int(sn))
    _detect_result_cache.clear()
    _refine_result_cache.clear()
    _identify_result_cache.clear()
    _manual_result_cache.clear()
    _manual_metadata_ref_cache = {}
    _manual_metadata_ref_built_mono = 0.0
    _photo_crop_index_cache = {}
    _photo_crop_index_built_mono = 0.0
    #Only the flagged serials' renders are stale; keeping the rest avoids
    #re-rendering the whole reference gallery from disk on every label change.
    if serials:
        _drop_ref_crop_renders_for_serials(list(serials))
    else:
        _ref_crop_result_cache.clear()
        _ref_crop_cache_keys_by_serial.clear()


def _ensure_flag_incorrect_queue_loaded() -> None:
    global _flag_incorrect_queue_loaded, _flag_incorrect_queue
    if _flag_incorrect_queue_loaded:
        return
    try:
        if _FLAG_INCORRECT_QUEUE_PATH.exists():
            raw = json.loads(_FLAG_INCORRECT_QUEUE_PATH.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
            loaded: Dict[int, Dict[str, Any]] = {}
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        sn = int(item.get("serial"))
                    except Exception:
                        continue
                    if sn <= 0:
                        continue
                    loaded[int(sn)] = {
                        "serial": int(sn),
                        "actor_name": str(item.get("actor_name") or ""),
                        "source_mode": str(item.get("source_mode") or ""),
                        "source_serial": _parse_serial(str(item.get("source_serial") or "")),
                        "source_crop": int(item.get("source_crop")) if str(item.get("source_crop") or "").strip().isdigit() else None,
                        "first_queued_ts": float(item.get("first_queued_ts") or item.get("queued_ts") or time.time()),
                        "updated_ts": float(item.get("updated_ts") or item.get("queued_ts") or time.time()),
                        "attempts": max(0, int(item.get("attempts") or 0)),
                        "last_attempt_ts": float(item.get("last_attempt_ts") or 0.0),
                        "last_error": str(item.get("last_error") or ""),
                    }
            _flag_incorrect_queue = loaded
    except Exception as e:
        log_action("labeler_flag_incorrect_queue_load_error", "error", f"{type(e).__name__}: {e!r}")
        _flag_incorrect_queue = {}
    _flag_incorrect_queue_loaded = True


def _persist_flag_incorrect_queue() -> None:
    try:
        _FLAG_INCORRECT_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FLAG_INCORRECT_QUEUE_PATH.with_suffix(".json.tmp")
        items: List[Dict[str, Any]] = []
        for sn in sorted(_flag_incorrect_queue.keys()):
            rec = _flag_incorrect_queue.get(int(sn)) or {}
            items.append(
                {
                    "serial": int(sn),
                    "actor_name": str(rec.get("actor_name") or ""),
                    "source_mode": str(rec.get("source_mode") or ""),
                    "source_serial": rec.get("source_serial"),
                    "source_crop": rec.get("source_crop"),
                    "first_queued_ts": float(rec.get("first_queued_ts") or 0.0),
                    "updated_ts": float(rec.get("updated_ts") or 0.0),
                    "attempts": max(0, int(rec.get("attempts") or 0)),
                    "last_attempt_ts": float(rec.get("last_attempt_ts") or 0.0),
                    "last_error": str(rec.get("last_error") or "")[:500],
                }
            )
        tmp.write_text(json.dumps(items, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_FLAG_INCORRECT_QUEUE_PATH)
    except Exception as e:
        log_action("labeler_flag_incorrect_queue_save_error", "error", f"{type(e).__name__}: {e!r}")


async def _remove_flag_incorrect_queue_serials(serials: List[int]) -> int:
    """Drop queued flag-clear jobs for serials that were re-labeled/unblacklisted."""
    want: Set[int] = set()
    for item in serials or []:
        try:
            sn = int(item)
        except Exception:
            continue
        if sn > 0:
            want.add(sn)
    if not want:
        return 0
    async with _flag_incorrect_queue_lock:
        _ensure_flag_incorrect_queue_loaded()
        removed = 0
        for sn in list(want):
            if int(sn) in _flag_incorrect_queue:
                _flag_incorrect_queue.pop(int(sn), None)
                removed += 1
        if removed:
            _persist_flag_incorrect_queue()
        return removed


async def _enqueue_flag_incorrect_job(
    serial: int,
    *,
    actor_name: str = "",
    source_mode: str = "",
    source_serial: Optional[int] = None,
    source_crop: Optional[int] = None,
) -> Dict[str, Any]:
    now_ts = time.time()
    queued_new = False
    pending_count = 0
    async with _flag_incorrect_queue_lock:
        _ensure_flag_incorrect_queue_loaded()
        existing = _flag_incorrect_queue.get(int(serial))
        queued_new = existing is None
        first_queued_ts = float(existing.get("first_queued_ts") or now_ts) if isinstance(existing, dict) else now_ts
        attempts = max(0, int(existing.get("attempts") or 0)) if isinstance(existing, dict) else 0
        _flag_incorrect_queue[int(serial)] = {
            "serial": int(serial),
            "actor_name": str(actor_name or ""),
            "source_mode": str(source_mode or ""),
            "source_serial": int(source_serial) if source_serial is not None else None,
            "source_crop": int(source_crop) if source_crop is not None else None,
            "first_queued_ts": first_queued_ts,
            "updated_ts": now_ts,
            "attempts": attempts,
            "last_attempt_ts": float(existing.get("last_attempt_ts") or 0.0) if isinstance(existing, dict) else 0.0,
            "last_error": "",
        }
        _persist_flag_incorrect_queue()
        pending_count = len(_flag_incorrect_queue)
    _kickoff_flag_incorrect_queue_worker()
    return {"queued_new": bool(queued_new), "pending_count": int(pending_count)}


def _kickoff_flag_incorrect_queue_worker() -> None:
    """Start the background metadata-clear worker if it is not already running."""
    global _flag_incorrect_worker_task
    try:
        if _flag_incorrect_worker_task and not _flag_incorrect_worker_task.done():
            return
        _flag_incorrect_worker_task = asyncio.create_task(_flag_incorrect_queue_worker())
    except Exception:
        pass


def _flush_flag_incorrect_queue_batch_sync(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply queued incorrect flags to the local metadata CSV."""
    serial_order: List[int] = []
    actor_by_serial: Dict[int, str] = {}
    for item in batch or []:
        if not isinstance(item, dict):
            continue
        try:
            sn = int(item.get("serial"))
        except Exception:
            continue
        if sn <= 0 or sn in actor_by_serial:
            continue
        serial_order.append(int(sn))
        actor_by_serial[int(sn)] = str(item.get("actor_name") or "")
    if not serial_order:
        return {"results": {}, "updated_metadata": False}
    actor_name = ""
    for sn in serial_order:
        candidate = actor_by_serial.get(int(sn), "").strip()
        if candidate:
            actor_name = candidate
            break
    outcome = local_photos.clear_metadata_annotations(serial_order, actor_name)
    return {"results": dict(outcome.get("results") or {}), "updated_metadata": bool(outcome.get("updated_metadata"))}


async def _flag_incorrect_queue_worker() -> None:
    """Drain queued incorrect flags in background with batched metadata updates."""
    while True:
        batch: List[Dict[str, Any]] = []
        async with _flag_incorrect_queue_lock:
            _ensure_flag_incorrect_queue_loaded()
            dirty = False
            # If a serial has been unblacklisted (re-labeled), drop any stale queued clear.
            for sn in list(_flag_incorrect_queue.keys()):
                if not _is_flagged_ref_serial(int(sn)):
                    _flag_incorrect_queue.pop(int(sn), None)
                    dirty = True
            if not _flag_incorrect_queue:
                if dirty:
                    _persist_flag_incorrect_queue()
                return

            ordered = sorted(
                _flag_incorrect_queue.values(),
                key=lambda rec: (
                    float(rec.get("updated_ts") or 0.0),
                    int(rec.get("serial") or 0),
                ),
            )
            now_ts = time.time()
            for rec in ordered:
                try:
                    sn = int(rec.get("serial"))
                except Exception:
                    continue
                if sn <= 0:
                    continue
                live = _flag_incorrect_queue.get(int(sn))
                if not isinstance(live, dict):
                    continue
                live["attempts"] = max(0, int(live.get("attempts") or 0)) + 1
                live["last_attempt_ts"] = now_ts
                batch.append(dict(live))
                if len(batch) >= _FLAG_INCORRECT_QUEUE_BATCH_MAX:
                    break
            if not batch:
                if dirty:
                    _persist_flag_incorrect_queue()
                return
            _persist_flag_incorrect_queue()

        try:
            outcome = await asyncio.to_thread(_flush_flag_incorrect_queue_batch_sync, batch)
        except Exception as e:
            err_txt = f"{type(e).__name__}: {e!r}"
            async with _flag_incorrect_queue_lock:
                _ensure_flag_incorrect_queue_loaded()
                touched = False
                for rec in batch:
                    try:
                        sn = int(rec.get("serial"))
                    except Exception:
                        continue
                    live = _flag_incorrect_queue.get(int(sn))
                    if not isinstance(live, dict):
                        continue
                    live["last_error"] = err_txt
                    live["updated_ts"] = time.time()
                    touched = True
                if touched:
                    _persist_flag_incorrect_queue()
            log_action("labeler_flag_incorrect_queue_flush_error", "error", err_txt)
            await asyncio.sleep(_FLAG_INCORRECT_QUEUE_RETRY_DELAY_SEC)
            continue

        result_map = outcome.get("results") if isinstance(outcome, dict) else {}
        if not isinstance(result_map, dict):
            result_map = {}
        success_serials: List[int] = []
        changed_serials: List[int] = []
        not_found_serials: List[int] = []
        async with _flag_incorrect_queue_lock:
            _ensure_flag_incorrect_queue_loaded()
            dirty = False
            for rec in batch:
                try:
                    sn = int(rec.get("serial"))
                except Exception:
                    continue
                res = result_map.get(int(sn)) or {}
                if bool(res.get("ok")):
                    _flag_incorrect_queue.pop(int(sn), None)
                    success_serials.append(int(sn))
                    if bool(res.get("changed")):
                        changed_serials.append(int(sn))
                    dirty = True
                    continue
                if bool(res.get("not_found")):
                    _flag_incorrect_queue.pop(int(sn), None)
                    not_found_serials.append(int(sn))
                    dirty = True
                    continue
                live = _flag_incorrect_queue.get(int(sn))
                if isinstance(live, dict):
                    live["last_error"] = str(res.get("error") or "Unknown queue flush failure")
                    live["updated_ts"] = time.time()
                    dirty = True
            if dirty:
                _persist_flag_incorrect_queue()
            pending_after = len(_flag_incorrect_queue)

        if success_serials:
            _invalidate_labeler_caches_after_label_clears(success_serials)
            _kickoff_photo_metadata_cache_refresh()
        if success_serials or not_found_serials:
            log_action(
                "labeler_flag_incorrect_queue_flush",
                f"ok={len(success_serials)}; changed={len(changed_serials)}; missing={len(not_found_serials)}",
                f"batch={len(batch)}; pending={pending_after}",
            )


def _filter_refs_for_flagged_serials(refs: Any) -> List[Any]:
    out: List[Any] = []
    if not isinstance(refs, list):
        return out
    for ref in refs:
        if not isinstance(ref, dict):
            out.append(ref)
            continue
        serial = ref.get("serial")
        if serial is not None and _is_flagged_ref_serial(serial):
            continue
        crop = ref.get("crop")
        try:
            serial_i = int(serial) if serial is not None and str(serial).strip() else None
            crop_i = int(crop) if crop is not None and str(crop).strip() else None
        except Exception:
            serial_i = None
            crop_i = None
    # If a ref claims a concrete metadata serial/crop but that crop no longer exists in the
    # current metadata state, treat it as stale gallery metadata and hide it.
        if (
            serial_i is not None
            and crop_i is not None
            and serial_i > 0
            and crop_i > 0
        and _photo_crop_index_cache
        and (int(serial_i), int(crop_i)) not in _photo_crop_index_cache
        ):
            continue
        out.append(ref)
    return out


def _box_cat_ids_has_reviewed_label(box_cat_ids: Any) -> bool:
    labels = [str(v or "").strip() for v in str(box_cat_ids or "").split("|")]
    for label in labels:
        if not label:
            continue
        if label.strip().lower() in _SKIP_CATID_LABELS:
            continue
        return True
    return False


def _has_reviewed_cat_label_token(label: Any) -> bool:
    token = str(label or "").strip().lower()
    return bool(token) and token not in _SKIP_CATID_LABELS


def _labeler_download_stats() -> str:
    """Expose stable warm diagnostics now that local photos are the source of truth."""
    return "active=0/0; inflight_tasks=0; total_started=0"


def _labeler_cache_inflight_count() -> int:
    """Legacy warm requests do not enqueue downloader work anymore."""
    return 0


def _labeler_cache_target_from_budget(budget_gb: float) -> int:
    """Estimate a warm target from currently available local photos."""
    del budget_gb
    try:
        available = len(local_photos.local_serials(force_refresh=False))
    except Exception:
        available = 0
    return max(10, available)


async def _refresh_local_photo_cache_state(
    queue: List[Dict[str, Any]],
    target_count: Optional[int] = None,
    *,
    scan_limit: Optional[int] = None,
    concurrency: int = 3,
) -> int:
    """Preserve warm-call behavior by pruning legacy mirrors and refreshing the local snapshot."""
    del queue, target_count, scan_limit, concurrency
    try:
        await asyncio.to_thread(labeler_cache.prune_legacy_image_cache)
    except Exception:
        pass
    try:
        serials = await asyncio.to_thread(local_photos.local_serials, force_refresh=False)
    except Exception:
        serials = set()
    return len(serials)


def _maybe_schedule_queue_cache_warm(mode: str, queue: List[Dict[str, Any]]) -> None:
    """Throttle repeated queue cache warm kicks to reduce redundant local disk churn."""
    if not queue:
        return
    if float(_loop_lag_ms) >= float(_LOOP_LAG_SHED_BG_WORK_MS):
        return
    key = str(mode or "").strip().lower()
    if key not in _queue_cache_warm_next_mono:
        key = "detect"
    now = time.monotonic()
    if now < float(_queue_cache_warm_next_mono.get(key, 0.0)):
        return
    _queue_cache_warm_next_mono[key] = now + float(_QUEUE_CACHE_WARM_COOLDOWN_SEC)
    warm_target = min(int(_QUEUE_CACHE_WARM_TARGET), len(queue))
    scan_limit = min(int(_QUEUE_CACHE_WARM_SCAN_LIMIT), len(queue))
    try:
        asyncio.create_task(
            _refresh_local_photo_cache_state(
                queue[:scan_limit],
                target_count=warm_target,
                scan_limit=scan_limit,
            )
        )
        asyncio.create_task(
            _warm_item_context_cache_for_items(
                queue[:scan_limit],
                force_raw=False,
            )
        )
    except Exception:
        pass


def _has_local_photo_serial(serial: Any) -> bool:
    try:
        sn = int(serial)
    except Exception:
        return False
    return sn > 0 and local_photos.has_local_photo(sn, force_refresh=False)


def _queue_items_from_rows(rows: List[List[str]], *, mode: str = "boot", max_items: int = 0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[int] = set()
    mode_s = str(mode or "boot").strip().lower()
    for row in rows[1:]:
        if len(row) <= COL_SERIAL:
            continue
        sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
        if sn is None or int(sn) <= 0:
            continue
        if int(sn) in seen:
            continue
        url = str(row[COL_URL] if len(row) > COL_URL else "").strip()
        box_coords = str(row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
        labels = str(row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "").strip()
        include = True
        if mode_s == "detect":
            include = (not box_coords) or (box_coords.lower() == "rejected")
        elif mode_s == "classify":
            include = bool(box_coords) and box_coords.lower() != "rejected"
        elif mode_s == "manual":
            include = bool(box_coords) and bool(labels)
        if not include:
            continue
        seen.add(int(sn))
        out.append({"serial": int(sn), "url": url})
        if max_items > 0 and len(out) >= int(max_items):
            break
    out.sort(key=lambda item: int(item.get("serial") or 0))
    return out


def _kickoff_boot_cache_warm_once() -> None:
    """Start one boot-time cache warm task (best effort)."""
    global _boot_cache_warm_task, _boot_cache_warm_started
    if not _BOOT_WARM_ENABLE:
        return
    if _boot_cache_warm_started:
        return
    if _boot_cache_warm_task and not _boot_cache_warm_task.done():
        return
    _boot_cache_warm_started = True

    async def _runner() -> None:
        try:
            rows = await _get_photo_metadata_rows_async(ttl_sec=120)
            boot_items = _queue_items_from_rows(rows, mode="boot", max_items=int(_BOOT_WARM_SCAN_LIMIT))
            if not boot_items:
                return
            target_guess = _labeler_cache_target_from_budget(float(_BOOT_WARM_BUDGET_GB))
            target = max(int(_QUEUE_CACHE_WARM_TARGET), min(int(target_guess), len(boot_items)))
            scan_limit = min(len(boot_items), int(_BOOT_WARM_SCAN_LIMIT))
            await asyncio.gather(
                _refresh_local_photo_cache_state(
                    boot_items,
                    target_count=target,
                    scan_limit=scan_limit,
                    concurrency=max(1, int(_BOOT_WARM_CONCURRENCY)),
                ),
                _warm_item_context_cache_for_items(
                    boot_items[:scan_limit],
                    force_raw=False,
                ),
                return_exceptions=True,
            )
            log_action(
                "labeler_boot_cache_warm_done",
                "boot",
                f"target={target}; scanned={min(len(boot_items), int(_BOOT_WARM_SCAN_LIMIT))}",
            )
        except Exception as e:
            log_action("labeler_boot_cache_warm_error", "error", f"{type(e).__name__}: {e!r}")

    try:
        _boot_cache_warm_task = asyncio.create_task(_runner())
    except Exception:
        _boot_cache_warm_task = None


async def _local_serials_async(*, force_refresh: bool = False) -> Set[int]:
    """Read the local serial snapshot without blocking the event loop."""
    return await asyncio.to_thread(local_photos.local_serials, force_refresh=force_refresh)


async def _log_local_mode_once() -> None:
    global _local_mode_logged
    if _local_mode_logged:
        return
    _local_mode_logged = True
    try:
        count = len(await _local_serials_async(force_refresh=False))
    except Exception:
        count = -1
    log_action(
        "labeler_local_mode",
        "startup",
        (
            f"local_only={1 if local_photos.is_local_only() else 0}; "
            f"root={str(local_photos.photo_root())}; "
            f"serials={int(count)}"
        ),
    )


def _parse_int_token(value: Any) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return 0


def _ttl_cache_get_text(
    cache: Dict[Any, Tuple[float, str]],
    key: Any,
    ttl_sec: float,
) -> str:
    rec = cache.get(key)
    if not rec:
        return ""
    ts, value = rec
    if (time.monotonic() - float(ts)) > float(ttl_sec):
        cache.pop(key, None)
        return ""
    return str(value or "").strip()


def _ttl_cache_set_text(
    cache: Dict[Any, Tuple[float, str]],
    key: Any,
    value: Any,
    *,
    max_items: int = _CONTEXT_CACHE_MAX_ITEMS,
) -> None:
    cache[key] = (time.monotonic(), str(value or "").strip())
    if len(cache) <= int(max_items):
        return
    overflow = len(cache) - int(max_items)
    if overflow <= 0:
        return
    oldest = sorted(cache.items(), key=lambda kv: float(kv[1][0]))[:overflow]
    for old_key, _ in oldest:
        cache.pop(old_key, None)


def _context_lookup_failed(key: Any) -> bool:
    """True while a Discord author/channel lookup is in negative-cache backoff."""
    until = _discord_context_unresolved.get(key)
    if until is None:
        return False
    if time.monotonic() >= float(until):
        _discord_context_unresolved.pop(key, None)
        return False
    return True


def _mark_context_lookup_failed(key: Any) -> None:
    """Remember an unresolvable author/channel so it is not re-fetched every pass.

    Deleted accounts and users who left the guild never resolve. Without this
    every queue load re-issued a REST fetch for each of them, and those fetches
    share the bot's rate limiter with the guild member lookup that gates login.
    """
    _discord_context_unresolved[key] = time.monotonic() + float(_DISCORD_CONTEXT_NEGATIVE_TTL_SEC)
    if len(_discord_context_unresolved) <= int(_CONTEXT_CACHE_MAX_ITEMS):
        return
    now = time.monotonic()
    for cached_key, until in list(_discord_context_unresolved.items()):
        if now >= float(until):
            _discord_context_unresolved.pop(cached_key, None)


def _get_labeler_bot_client() -> Any:
    for mod_name in ("tomcat.main", "__main__", "main"):
        mod = sys.modules.get(mod_name)
        client = getattr(mod, "bot", None) if mod is not None else None
        if client is not None:
            return client
    for mod in list(sys.modules.values()):
        try:
            if mod is None:
                continue
            mod_file = str(getattr(mod, "__file__", "") or "").replace("/", "\\").lower()
            if not mod_file.endswith("\\tomcat\\main.py"):
                continue
            client = getattr(mod, "bot", None)
            if client is not None:
                return client
        except Exception:
            continue
    try:
        from .. import main as main_mod

        return getattr(main_mod, "bot", None)
    except Exception:
        return None


def _build_photo_item_context_cache_sync() -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    try:
        rows = local_photos.read_metadata_rows()
    except Exception:
        return out
    for row in rows or []:
        serial = _parse_serial(str((row or {}).get("Serial Number", "") or ""))
        if serial is None or int(serial) <= 0:
            continue
        out[int(serial)] = {
            "serial": int(serial),
            "author_id": str((row or {}).get("Author ID", "") or "").strip(),
            "channel_id": str((row or {}).get("Channel", "") or "").strip(),
            "guild_id": str((row or {}).get("Guild ID", "") or "").strip(),
            "message_id": str((row or {}).get("Message ID", "") or "").strip(),
            "timestamp": str((row or {}).get("Timestamp", "") or "").strip(),
            "url": str((row or {}).get("Discord URL", "") or "").strip(),
        }
    return out


async def _get_photo_item_context_cache_async(
    *,
    force: bool = False,
    serials: Optional[List[int]] = None,
) -> Dict[int, Dict[str, Any]]:
    global _photo_item_context_cache, _photo_item_context_cache_built_mono
    now_mono = time.monotonic()
    clean_serials = [
        int(sn)
        for sn in list(serials or [])
        if isinstance(sn, int) and int(sn) > 0
    ]
    needs_refresh = bool(force) or not _photo_item_context_cache
    if not needs_refresh:
        age_sec = now_mono - float(_photo_item_context_cache_built_mono or 0.0)
        needs_refresh = age_sec >= float(_PHOTO_ITEM_CONTEXT_CACHE_TTL_SEC)
    if not needs_refresh and clean_serials:
        needs_refresh = any(int(sn) not in _photo_item_context_cache for sn in clean_serials)
    if not needs_refresh:
        return _photo_item_context_cache
    async with _photo_item_context_cache_lock:
        now_mono = time.monotonic()
        needs_refresh = bool(force) or not _photo_item_context_cache
        if not needs_refresh:
            age_sec = now_mono - float(_photo_item_context_cache_built_mono or 0.0)
            needs_refresh = age_sec >= float(_PHOTO_ITEM_CONTEXT_CACHE_TTL_SEC)
        if not needs_refresh and clean_serials:
            needs_refresh = any(int(sn) not in _photo_item_context_cache for sn in clean_serials)
        if needs_refresh:
            _photo_item_context_cache = await asyncio.to_thread(_build_photo_item_context_cache_sync)
            _photo_item_context_cache_built_mono = time.monotonic()
    return _photo_item_context_cache


async def _ensure_labeler_bot_ready() -> Any:
    client = _get_labeler_bot_client()
    if client is None:
        return None
    try:
        if hasattr(client, "is_ready") and not client.is_ready():
            await asyncio.wait_for(client.wait_until_ready(), timeout=2.0)
    except Exception:
        pass
    return client


async def _resolve_author_display_name(
    guild_id: Any,
    author_id: Any,
    *,
    allow_fetch: bool = True,
) -> str:
    uid = _parse_int_token(author_id)
    gid = _parse_int_token(guild_id)
    if uid <= 0:
        return ""
    if gid > 0:
        cached_member = _ttl_cache_get_text(
            _discord_member_display_cache,
            (int(gid), int(uid)),
            _DISCORD_CONTEXT_CACHE_TTL_SEC,
        )
        if cached_member:
            return cached_member
    cached_user = _ttl_cache_get_text(
        _discord_user_display_cache,
        int(uid),
        _DISCORD_CONTEXT_CACHE_TTL_SEC,
    )
    if cached_user and (gid <= 0 or not allow_fetch):
        return cached_user
    if not allow_fetch:
        return cached_user
    if _context_lookup_failed(("author", int(gid), int(uid))):
        return cached_user

    client = await _ensure_labeler_bot_ready()
    if client is None:
        return cached_user

    display = ""
    if gid > 0:
        guild = client.get_guild(int(gid))
        if guild is not None:
            member = guild.get_member(int(uid))
            if member is None:
                try:
                    member = await guild.fetch_member(int(uid))
                except Exception:
                    member = None
            if member is not None:
                display = (
                    str(getattr(member, "display_name", None) or "").strip()
                    or str(getattr(member, "global_name", None) or "").strip()
                    or str(getattr(member, "name", None) or "").strip()
                )
        if display:
            _ttl_cache_set_text(_discord_member_display_cache, (int(gid), int(uid)), display)
            _ttl_cache_set_text(_discord_user_display_cache, int(uid), display)
            return display

    user = client.get_user(int(uid))
    if user is None and hasattr(client, "fetch_user"):
        try:
            user = await client.fetch_user(int(uid))
        except Exception:
            user = None
    if user is not None:
        display = (
            str(getattr(user, "global_name", None) or "").strip()
            or str(getattr(user, "name", None) or "").strip()
        )
        if display:
            _ttl_cache_set_text(_discord_user_display_cache, int(uid), display)
            if gid > 0:
                _ttl_cache_set_text(_discord_member_display_cache, (int(gid), int(uid)), display)
            return display
    if cached_user and gid > 0:
        _ttl_cache_set_text(_discord_member_display_cache, (int(gid), int(uid)), cached_user)
        return cached_user
    #Nothing resolved: back off so the next queue load does not re-fetch this
    #author from Discord all over again.
    _mark_context_lookup_failed(("author", int(gid), int(uid)))
    return cached_user


async def _resolve_channel_name(channel_id: Any, *, allow_fetch: bool = True) -> str:
    cid = _parse_int_token(channel_id)
    if cid <= 0:
        return ""
    cached = _ttl_cache_get_text(
        _discord_channel_name_cache,
        int(cid),
        _DISCORD_CONTEXT_CACHE_TTL_SEC,
    )
    if cached or not allow_fetch:
        return cached
    if _context_lookup_failed(("channel", int(cid))):
        return ""

    client = await _ensure_labeler_bot_ready()
    if client is None:
        return ""

    channel = client.get_channel(int(cid))
    if channel is None and hasattr(client, "fetch_channel"):
        try:
            channel = await client.fetch_channel(int(cid))
        except Exception:
            channel = None
    if channel is None:
        _mark_context_lookup_failed(("channel", int(cid)))
        return ""
    name = str(getattr(channel, "name", None) or "").strip() or str(channel).strip()
    if name:
        _ttl_cache_set_text(_discord_channel_name_cache, int(cid), name)
    else:
        _mark_context_lookup_failed(("channel", int(cid)))
    return name


def _build_item_context_payload_from_caches(
    serial: int,
    raw_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "serial": int(serial),
        "author_id": "",
        "author_display_name": "",
        "channel_id": "",
        "channel_name": "",
        "guild_id": "",
        "message_id": "",
        "timestamp": "",
        "has_context": False,
    }
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    if not meta:
        return payload
    payload["has_context"] = True
    payload["author_id"] = str(meta.get("author_id") or "").strip()
    payload["channel_id"] = str(meta.get("channel_id") or "").strip()
    payload["guild_id"] = str(meta.get("guild_id") or "").strip()
    payload["message_id"] = str(meta.get("message_id") or "").strip()
    payload["timestamp"] = str(meta.get("timestamp") or "").strip()

    uid = _parse_int_token(payload["author_id"])
    gid = _parse_int_token(payload["guild_id"])
    cid = _parse_int_token(payload["channel_id"])
    if gid > 0 and uid > 0:
        payload["author_display_name"] = _ttl_cache_get_text(
            _discord_member_display_cache,
            (int(gid), int(uid)),
            _DISCORD_CONTEXT_CACHE_TTL_SEC,
        )
    if not payload["author_display_name"] and uid > 0:
        payload["author_display_name"] = _ttl_cache_get_text(
            _discord_user_display_cache,
            int(uid),
            _DISCORD_CONTEXT_CACHE_TTL_SEC,
        )
    if cid > 0:
        payload["channel_name"] = _ttl_cache_get_text(
            _discord_channel_name_cache,
            int(cid),
            _DISCORD_CONTEXT_CACHE_TTL_SEC,
        )
    return payload


def _apply_item_context_to_items(
    items: List[Dict[str, Any]],
    raw_context_cache: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        row = dict(item or {})
        try:
            serial = int(row.get("serial") or 0)
        except Exception:
            serial = 0
        if serial > 0:
            ctx = _build_item_context_payload_from_caches(serial, raw_context_cache.get(int(serial)))
            for field in (
                "author_id",
                "author_display_name",
                "channel_id",
                "channel_name",
                "guild_id",
                "message_id",
                "timestamp",
                "has_context",
            ):
                if field == "has_context":
                    row[field] = bool(ctx.get(field))
                    continue
                value = str(ctx.get(field) or "").strip()
                if value:
                    row[field] = value
        out.append(row)
    return out


async def _warm_item_context_cache_for_items(
    items: List[Dict[str, Any]],
    *,
    force_raw: bool = False,
) -> None:
    serials: List[int] = []
    seen_serials: Set[int] = set()
    for item in items or []:
        try:
            serial = int(item.get("serial") or 0)
        except Exception:
            serial = 0
        if serial <= 0 or serial in seen_serials:
            continue
        seen_serials.add(serial)
        serials.append(int(serial))
    if not serials:
        return

    raw_context_cache = await _get_photo_item_context_cache_async(force=force_raw, serials=serials)
    author_targets: List[Tuple[int, int]] = []
    channel_targets: List[int] = []
    seen_authors: Set[Tuple[int, int]] = set()
    seen_channels: Set[int] = set()
    for serial in serials:
        meta = raw_context_cache.get(int(serial)) or {}
        uid = _parse_int_token(meta.get("author_id"))
        gid = _parse_int_token(meta.get("guild_id"))
        cid = _parse_int_token(meta.get("channel_id"))
        if uid > 0:
            author_key = (int(gid), int(uid))
            if author_key not in seen_authors:
                seen_authors.add(author_key)
                author_targets.append(author_key)
        if cid > 0 and cid not in seen_channels:
            seen_channels.add(int(cid))
            channel_targets.append(int(cid))

    sem = asyncio.Semaphore(_CONTEXT_WARM_CONCURRENCY)

    async def _warm_author(gid: int, uid: int) -> None:
        async with sem:
            try:
                await _resolve_author_display_name(int(gid), int(uid), allow_fetch=True)
            except Exception:
                pass

    async def _warm_channel(cid: int) -> None:
        async with sem:
            try:
                await _resolve_channel_name(int(cid), allow_fetch=True)
            except Exception:
                pass

    tasks: List[asyncio.Future] = []
    for gid, uid in author_targets:
        if gid > 0:
            member_cached = _ttl_cache_get_text(
                _discord_member_display_cache,
                (int(gid), int(uid)),
                _DISCORD_CONTEXT_CACHE_TTL_SEC,
            )
            if member_cached:
                continue
        user_cached = _ttl_cache_get_text(
            _discord_user_display_cache,
            int(uid),
            _DISCORD_CONTEXT_CACHE_TTL_SEC,
        )
        if _context_lookup_failed(("author", int(gid), int(uid))):
            continue
        if not user_cached or gid > 0:
            tasks.append(asyncio.create_task(_warm_author(int(gid), int(uid))))
    for cid in channel_targets:
        cached = _ttl_cache_get_text(
            _discord_channel_name_cache,
            int(cid),
            _DISCORD_CONTEXT_CACHE_TTL_SEC,
        )
        if cached:
            continue
        if _context_lookup_failed(("channel", int(cid))):
            continue
        tasks.append(asyncio.create_task(_warm_channel(int(cid))))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _get_resolved_item_context_payload(
    serial: int,
    *,
    force_raw: bool = False,
    allow_fetch: bool = True,
) -> Dict[str, Any]:
    clean_serial = int(serial)
    raw_context_cache = await _get_photo_item_context_cache_async(
        force=force_raw,
        serials=[int(clean_serial)],
    )
    if allow_fetch:
        await _warm_item_context_cache_for_items(
            [{"serial": int(clean_serial)}],
            force_raw=force_raw,
        )
        raw_context_cache = await _get_photo_item_context_cache_async(serials=[int(clean_serial)])
    return _build_item_context_payload_from_caches(
        int(clean_serial),
        raw_context_cache.get(int(clean_serial)),
    )


def _normalize_header_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").strip().lower())


def _find_header_col(headers: List[str], candidates: List[str]) -> Optional[int]:
    if not headers:
        return None
    wanted = {_normalize_header_token(c) for c in candidates}
    for idx, header in enumerate(headers):
        if _normalize_header_token(header) in wanted:
            return idx
    return None


def _col_to_a1(col_index_1_based: int) -> str:
    n = int(col_index_1_based)
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _merge_labeled_by(existing: str, actor: str) -> str:
    actor_clean = str(actor or "").strip()
    if not actor_clean:
        return str(existing or "").strip()
    names: List[str] = []
    seen = set()
    for tok in str(existing or "").split(","):
        name = tok.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    if actor_clean.casefold() not in seen:
        names.append(actor_clean)
    return ", ".join(names)


def _actor_from_request(request: web.Request) -> Tuple[str, str]:
    session = request.get("tc_session") or {}
    user_id = str(session.get("user_id") or "").strip()
    username = str(session.get("username") or "").strip()
    global_name = str(session.get("global_name") or "").strip()
    actor = username or global_name or user_id or "unknown"
    return user_id, actor


def _kick_detector_warm_task() -> None:
    """Fire-and-forget detector warmup once per process."""
    global _detector_warm_task, _detector_warm_done
    if _detector_warm_done:
        return
    if _detector_warm_task and not _detector_warm_task.done():
        return

    async def _runner() -> None:
        try:
            await asyncio.to_thread(V.warm_labeler_detector)
        except Exception as e:
            log_action("labeler_detector_warm_error", f"type={type(e).__name__}", str(e))
        finally:
            _detector_warm_done = True

    _detector_warm_task = asyncio.create_task(_runner())


def _norm_cat_lookup_token(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    m = _CAT_ID_NAME_RE.match(raw)
    if m:
        raw = m.group(2).strip()
    return re.sub(r"[^a-z0-9]+", "", raw.lower())


def _alias_lookup_meta(
    alias_lookup: Dict[str, Dict[str, Any]],
    token: str,
) -> Optional[Dict[str, Any]]:
    key = str(token or "").strip()
    if not key:
        return None
    meta = alias_lookup.get(key)
    if meta:
        return meta
    if key.endswith("e"):
        alt = key[:-1]
    else:
        alt = f"{key}e"
    if alt:
        meta = alias_lookup.get(alt)
        if meta:
            return meta
    return None


def _parse_cat_full_name(full_name: str) -> Optional[Tuple[int, str]]:
    s = str(full_name or "").strip()
    if not s:
        return None
    m = _CAT_ID_NAME_RE.match(s)
    if not m:
        return None
    try:
        return int(m.group(1)), m.group(2).strip()
    except Exception:
        return None


def _build_catid_lookup(cat_rows: List[List[str]]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for row in cat_rows[1:]:
        if not row:
            continue
        full_name = str(row[0] if len(row) > 0 else "").strip()
        parsed = _parse_cat_full_name(full_name)
        if not parsed:
            continue
        cid, name = parsed
        canonical = f"{cid}. {name}"
        key_full = _norm_cat_lookup_token(full_name)
        key_name = _norm_cat_lookup_token(name)
        if key_full and key_full not in lookup:
            lookup[key_full] = canonical
        if key_name and key_name not in lookup:
            lookup[key_name] = canonical
    return lookup


def _format_catid_cell_from_labels(box_cat_ids: str, lookup: Dict[str, str]) -> str:
    out: List[str] = []
    seen = set()
    for raw in str(box_cat_ids or "").split("|"):
        token = str(raw or "").strip()
        if not token:
            continue
        if token.strip().lower() in _SKIP_CATID_LABELS:
            continue
        key = _norm_cat_lookup_token(token)
        mapped = lookup.get(key, "")
        if not mapped:
            parsed = _parse_cat_full_name(token)
            if parsed:
                mapped = f"{parsed[0]}. {parsed[1]}"
        if not mapped:
            continue
        marker = mapped.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        out.append(mapped)
    return ", ".join(out)


def _is_needs_review_label(label: str) -> bool:
    return str(label or "").strip().lower() in _NEEDS_REVIEW_LABELS


def _parse_yolo_box_str(box_str: str) -> Optional[Tuple[float, float, float, float]]:
    try:
        parts = [float(p) for p in str(box_str or "").strip().split()]
    except Exception:
        return None
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _load_profile_catalog() -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Return alias lookup + ordered cats + canonical-key map from CatDatabase cache."""
    global _profile_refresh_mono
    alias_lookup: Dict[str, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    try:
        from ..services import profile_cache
        now_mono = time.monotonic()
        if (now_mono - float(_profile_refresh_mono or 0.0)) >= _PROFILE_REFRESH_MIN_SEC:
            try:
                profile_cache.refresh_sync()
            except Exception:
                pass
            _profile_refresh_mono = now_mono
        full_names = profile_cache.all_actual_names()
        for full in full_names:
            raw = str(full or "").strip()
            if not raw:
                continue
            parsed = _parse_cat_full_name(raw)
            cat_id: Optional[int] = None
            name = raw
            if parsed:
                cat_id = int(parsed[0])
                name = parsed[1].strip()
            key = _norm_cat_lookup_token(name)
            if not key:
                continue
            if key in {"notacat", "needsreview", "rejected"}:
                continue
            if key in by_key:
                continue
            display = f"{cat_id}. {name}" if cat_id is not None else name
            desc = ""
            try:
                prof = profile_cache.get_profile_local(name) or {}
                desc = str(prof.get("physical_description") or prof.get("physical") or "").strip()
            except Exception:
                desc = ""
            meta = {
                "key": key,
                "name": name,
                "cat_id": cat_id,
                "display_name": display,
                "desc": desc,
            }
            by_key[key] = meta
            ordered.append(meta)
            alias_tokens = [name, raw, display]
            for tok in alias_tokens:
                tkey = _norm_cat_lookup_token(tok)
                if tkey and tkey not in alias_lookup:
                    alias_lookup[tkey] = meta
    except Exception:
        return alias_lookup, ordered, by_key

    # Include gallery-only cats so fallback refs can still resolve when CatDatabase lags behind gallery.
    try:
        for gname in V.get_all_cats() or []:
            name = str(gname or "").strip()
            if not name:
                continue
            key = _norm_cat_lookup_token(name)
            if not key or key in {"notacat", "needsreview", "rejected"}:
                continue
            if key in by_key:
                continue
            meta = {
                "key": key,
                "name": name,
                "cat_id": None,
                "display_name": name,
                "desc": "",
            }
            by_key[key] = meta
            ordered.append(meta)
            if key not in alias_lookup:
                alias_lookup[key] = meta
    except Exception:
        pass

    ordered.sort(key=lambda m: (m.get("cat_id") is None, int(m.get("cat_id") or 10**9), str(m.get("name") or "")))
    return alias_lookup, ordered, by_key


def _parse_catid_cell_names(cell_value: str) -> List[str]:
    out: List[str] = []
    for tok in re.split(r"[|,;/]+", str(cell_value or "")):
        t = str(tok or "").strip()
        if not t:
            continue
        parsed = _parse_cat_full_name(t)
        if parsed:
            out.append(parsed[1].strip())
        else:
            out.append(t)
    return out


def _manual_metadata_ref_reservoir_add(
    refs: Dict[str, List[Dict[str, Any]]],
    counts: Dict[str, int],
    cat_key: str,
    entry: Dict[str, Any],
    *,
    limit: Optional[int] = None,
) -> None:
    if not cat_key:
        return
    lim = max(1, int(limit if limit is not None else _MANUAL_METADATA_REF_SAMPLE_PER_CAT))
    bucket = refs.setdefault(cat_key, [])
    seen_n = int(counts.get(cat_key, 0)) + 1
    counts[cat_key] = seen_n
    if len(bucket) < lim:
        bucket.append(entry)
        return
    j = random.randint(1, seen_n)
    if j <= lim:
        bucket[j - 1] = entry


def _build_manual_metadata_ref_cache(alias_lookup: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    rows = get_photo_metadata_rows(ttl_sec=_queue_rows_ttl_sec())
    refs: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}

    for row in rows[1:]:
        if len(row) <= COL_URL:
            continue
        serial = _parse_serial(str(row[COL_SERIAL] if len(row) > COL_SERIAL else ""))
        if serial is None or not _has_local_photo_serial(serial):
            continue
        url = str(row[COL_URL] if len(row) > COL_URL else "").strip()
        if _is_flagged_ref_serial(serial):
            continue
        box_coords = str(row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
        box_cat_ids = str(row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "").strip()
        catid_cell = str(row[COL_CAT_ID] if len(row) > COL_CAT_ID else "").strip()

        coords = [c.strip() for c in box_coords.split("|") if c.strip()] if box_coords and box_coords.lower() != "rejected" else []
        # Keep positional alignment with coords (do not drop empty labels).
        labels = [l.strip() for l in box_cat_ids.split("|")] if box_cat_ids else []

        # Primary source: explicit per-box labels.
        for i in range(min(len(coords), len(labels))):
            label = labels[i]
            low = label.strip().lower()
            if low in _SKIP_CATID_LABELS:
                continue
            token = _norm_cat_lookup_token(label)
            if not token:
                continue
            meta = _alias_lookup_meta(alias_lookup, token)
            cat_key = str((meta or {}).get("key") or token).strip()
            if cat_key in {"notacat", "needsreview", "rejected"}:
                continue
            _manual_metadata_ref_reservoir_add(
                refs,
                counts,
                cat_key,
                {
                    "serial": serial,
                    "url": url,
                    "box": coords[i],
                    "crop": i + 1,
                    "source": "box_cat_ids",
                },
                limit=_MANUAL_METADATA_REF_CROPPED_SAMPLE_PER_CAT,
            )

        catid_names = _parse_catid_cell_names(catid_cell)
        catid_metas: List[Dict[str, Any]] = []
        seen_keys = set()
        for name in catid_names:
            token = _norm_cat_lookup_token(name)
            if not token:
                continue
            meta = _alias_lookup_meta(alias_lookup, token)
            key = str((meta or {}).get("key") or token).strip()
            if not key or key in {"notacat", "needsreview", "rejected"} or key in seen_keys:
                continue
            seen_keys.add(key)
            catid_metas.append(meta or {"key": key})

        # Heuristic: single CatID + single box => treat that box as this cat.
        if len(coords) == 1 and len(catid_metas) == 1:
            meta = catid_metas[0]
            _manual_metadata_ref_reservoir_add(
                refs,
                counts,
                str(meta.get("key") or ""),
                {
                    "serial": serial,
                    "url": url,
                    "box": coords[0],
                    "crop": 1,
                    "source": "catid_single_box",
                },
                limit=_MANUAL_METADATA_REF_CROPPED_SAMPLE_PER_CAT,
            )

        # Fallback: uncropped row image by CatID.
        for meta in catid_metas:
            _manual_metadata_ref_reservoir_add(
                refs,
                counts,
                str(meta.get("key") or ""),
                {
                    "serial": serial,
                    "url": url,
                    "box": "",
                    "crop": None,
                    "source": "catid_uncropped",
                },
                limit=_MANUAL_METADATA_REF_UNCROPPED_SAMPLE_PER_CAT,
            )

    return refs


def _build_photo_crop_index_cache() -> Dict[Tuple[int, int], Dict[str, Any]]:
    rows = get_photo_metadata_rows(ttl_sec=_queue_rows_ttl_sec())
    out: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for row in rows[1:]:
        if len(row) <= COL_URL:
            continue
        serial = _parse_serial(str(row[COL_SERIAL] if len(row) > COL_SERIAL else ""))
        if serial is None or not _has_local_photo_serial(serial):
            continue
        url = str(row[COL_URL] if len(row) > COL_URL else "").strip()
        if _is_flagged_ref_serial(serial):
            continue
        box_coords = str(row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
        box_cat_ids = str(row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "").strip()
        catid_cell = str(row[COL_CAT_ID] if len(row) > COL_CAT_ID else "").strip()
        if not box_coords or box_coords.lower() == "rejected":
            continue
        coords = [c.strip() for c in box_coords.split("|")]
        labels = [l.strip() for l in box_cat_ids.split("|")] if box_cat_ids else []

        valid_coords: List[Tuple[int, str]] = []
        for i, coord in enumerate(coords):
            if not coord or _parse_yolo_box_str(coord) is None:
                continue
            valid_coords.append((i, coord))
        if not valid_coords:
            continue

        # Only expose crops that are still actively labeled.
        # Ignore crops whose labels were cleared so the reference gallery only
        # shows active annotations.
        single_box_cid_fallback = False
        if len(valid_coords) == 1:
            active_cats = [n for n in _parse_catid_cell_names(catid_cell) if _has_reviewed_cat_label_token(n)]
            single_box_cid_fallback = len(active_cats) == 1

        for i, coord in valid_coords:
            has_box_label = i < len(labels) and _has_reviewed_cat_label_token(labels[i])
            if not has_box_label and not single_box_cid_fallback:
                continue
            crop_num = i + 1
            key = (int(serial), int(crop_num))
            if key in out:
                continue
            out[key] = {
                "serial": int(serial),
                "crop": int(crop_num),
                "url": url,
                "box": coord,
                "label_source": "box_cat_ids" if has_box_label else "catid_single_box",
            }
    return out


async def _ensure_photo_crop_index_cache(force: bool = False) -> None:
    global _photo_crop_index_cache, _photo_crop_index_built_mono
    global _photo_crop_index_generation
    now = time.monotonic()
    if (
        not force
        and _photo_crop_index_cache
        and (now - float(_photo_crop_index_built_mono)) < _MANUAL_METADATA_REF_TTL_SEC
    ):
        return
    if (
        force
        and _photo_crop_index_cache
        and (now - float(_photo_crop_index_built_mono)) < _PHOTO_CROP_INDEX_FORCE_COALESCE_SEC
    ):
        return
    #A forced rebuild scans the whole photo metadata table, and the UI fetches
    #ref crops a dozen at a time, so a single batch of misses used to queue a
    #dozen full rebuilds back to back.
    requested_generation = _photo_crop_index_generation
    async with _photo_crop_index_lock:
        now2 = time.monotonic()
        if (
            not force
            and _photo_crop_index_cache
            and (now2 - float(_photo_crop_index_built_mono)) < _MANUAL_METADATA_REF_TTL_SEC
        ):
            return
        if (
            force
            and _photo_crop_index_cache
            and (now2 - float(_photo_crop_index_built_mono)) < _PHOTO_CROP_INDEX_FORCE_COALESCE_SEC
        ):
            return
        if force and float(_photo_crop_index_built_mono) > requested_mono:
            #Somebody else rebuilt while this caller waited for the lock, so the
            #index is already newer than the miss that triggered this call.
            return
        _photo_crop_index_cache = await asyncio.to_thread(_build_photo_crop_index_cache)
        _photo_crop_index_built_mono = time.monotonic()
        _photo_crop_index_generation += 1


async def _refresh_photo_crop_index_after_miss() -> None:
    """Rebuild the crop index for a cache miss, at most once per interval.

    A crop that is genuinely absent from the metadata misses forever, so
    rebuilding on every miss meant a steady stream of full table scans for
    serials that were never going to appear. Explicit warm requests still
    force unconditionally.
    """
    global _photo_crop_index_miss_rebuild_next_mono
    now = time.monotonic()
    if now < float(_photo_crop_index_miss_rebuild_next_mono):
        return
    _photo_crop_index_miss_rebuild_next_mono = now + float(_PHOTO_CROP_INDEX_MISS_REBUILD_INTERVAL_SEC)
    await _ensure_photo_crop_index_cache(force=True)


def _photo_ref_crop_url(serial: int, crop: int) -> str:
    return f"/api/labeler/ref_crop/{int(serial)}/{int(crop)}"


def _map_identify_candidate_refs_to_metadata(
    refs: List[Dict[str, Any]],
    *,
    refs_per: int,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[Tuple[int, int]] = set()
    limit = max(1, int(refs_per or 1))
    valid: List[Tuple[int, int, str]] = []
    for row in refs or []:
        if not isinstance(row, dict):
            continue
        try:
            serial = int(row.get("serial"))
            crop = int(row.get("crop"))
        except Exception:
            continue
        if serial <= 0 or crop <= 0:
            continue
        if _is_flagged_ref_serial(serial):
            continue
        key = (serial, crop)
        if key in seen:
            continue
        # Skip known-bad refs to avoid repeated fetch errors and log spam.
        bad_until = _ref_crop_negative_cache.get(key, 0.0)
        if bad_until and time.monotonic() < float(bad_until):
            continue
        entry = _photo_crop_index_cache.get(key) or {}
        if not entry:
            continue
        seen.add(key)
        box = str(entry.get("box") or "").strip()
        valid.append((serial, crop, box))

    if not valid:
        return out

    cached_first: List[Tuple[int, int, str]] = []
    uncached: List[Tuple[int, int, str]] = []
    for serial, crop, box in valid:
        is_cached = local_photos.has_local_photo(int(serial), force_refresh=False)
        if is_cached:
            cached_first.append((serial, crop, box))
        else:
            uncached.append((serial, crop, box))

    ordered = cached_first + uncached
    for serial, crop, box in ordered:
        out.append({
            "img": "",
            "url": _photo_ref_crop_url(serial, crop),
            "serial": serial,
            "crop": crop,
            "box": box,
            "source": "photo_crop",
        })
        if len(out) >= limit:
            break
    return out


async def _ensure_manual_metadata_ref_cache(alias_lookup: Dict[str, Dict[str, Any]], force: bool = False) -> None:
    global _manual_metadata_ref_cache, _manual_metadata_ref_built_mono
    now = time.monotonic()
    if (
        not force
        and _manual_metadata_ref_cache
        and (now - float(_manual_metadata_ref_built_mono)) < _MANUAL_METADATA_REF_TTL_SEC
    ):
        return
    async with _manual_metadata_ref_lock:
        now2 = time.monotonic()
        if (
            not force
            and _manual_metadata_ref_cache
            and (now2 - float(_manual_metadata_ref_built_mono)) < _MANUAL_METADATA_REF_TTL_SEC
        ):
            return
        _manual_metadata_ref_cache = await asyncio.to_thread(_build_manual_metadata_ref_cache, alias_lookup)
        _manual_metadata_ref_built_mono = time.monotonic()


def _fallback_refs_for_cat(
    cat_key: str,
    limit: int = _MANUAL_FALLBACK_REFS_PER_CAT,
    *,
    include_uncropped: bool = True,
    prefer_cached: bool = False,
    prefer_serial: Optional[int] = None,
) -> List[Dict[str, Any]]:
    entries = list(_manual_metadata_ref_cache.get(str(cat_key or ""), []) or [])
    if entries:
        entries = [
            e for e in entries
            if not _is_flagged_ref_serial(e.get("serial"))
        ]
    if not entries:
        return []
    cropped = [e for e in entries if str(e.get("box") or "").strip()]
    uncropped = [e for e in entries if not str(e.get("box") or "").strip()]
    def _serial_distance(ent: Dict[str, Any]) -> int:
        if prefer_serial is None:
            return 0
        try:
            sn = int(ent.get("serial"))
            return abs(int(sn) - int(prefer_serial))
        except Exception:
            return 10**9

    if prefer_cached:
        cropped_cached: List[Dict[str, Any]] = []
        cropped_uncached: List[Dict[str, Any]] = []
        uncropped_cached: List[Dict[str, Any]] = []
        uncropped_uncached: List[Dict[str, Any]] = []

        def _is_cached_entry(ent: Dict[str, Any]) -> bool:
            sn = ent.get("serial")
            if sn is None:
                return False
            return local_photos.has_local_photo(int(sn), force_refresh=False)

        for ent in cropped:
            if _is_cached_entry(ent):
                cropped_cached.append(ent)
            else:
                cropped_uncached.append(ent)
        for ent in uncropped:
            if _is_cached_entry(ent):
                uncropped_cached.append(ent)
            else:
                uncropped_uncached.append(ent)

        if prefer_serial is None:
            random.shuffle(cropped_cached)
            random.shuffle(cropped_uncached)
            random.shuffle(uncropped_cached)
            random.shuffle(uncropped_uncached)
        else:
            cropped_cached.sort(key=_serial_distance)
            cropped_uncached.sort(key=_serial_distance)
            uncropped_cached.sort(key=_serial_distance)
            uncropped_uncached.sort(key=_serial_distance)
        cached_pool = list(cropped_cached)
        if include_uncropped:
            cached_pool.extend(uncropped_cached)
        # If we already have enough cached refs, avoid uncached refs that commonly fail to render.
        if len(cached_pool) >= max(1, int(limit or 1)):
            pool = cached_pool
        else:
            pool = list(cached_pool)
            pool.extend(cropped_uncached)
            if include_uncropped:
                pool.extend(uncropped_uncached)
    else:
        if prefer_serial is None:
            random.shuffle(cropped)
            random.shuffle(uncropped)
        else:
            cropped.sort(key=_serial_distance)
            uncropped.sort(key=_serial_distance)
        pool = list(cropped)
        if include_uncropped:
            pool.extend(uncropped)
    chosen = pool[: max(1, int(limit or 1))]
    refs: List[Dict[str, Any]] = []
    for ent in chosen:
        serial = ent.get("serial")
        url = str(ent.get("url") or "").strip()
        crop_raw = ent.get("crop")
        crop_num: Optional[int] = None
        try:
            if crop_raw is not None and str(crop_raw).strip():
                crop_num = int(crop_raw)
        except Exception:
            crop_num = None
        ref_url = ""
        try:
            if serial is not None and crop_num is not None and crop_num > 0:
                ref_url = _photo_ref_crop_url(int(serial), int(crop_num))
        except Exception:
            ref_url = ""
        if not ref_url:
            ref_url = url
        if not ref_url and serial is not None:
            ref_url = f"/api/labeler/cached_image/{int(serial)}"
        refs.append({
            "img": "",
            "url": ref_url,
            "serial": serial,
            "crop": crop_num if crop_num is not None else ent.get("crop"),
            "box": ent.get("box") or "",
            "source": ent.get("source") or "fallback",
        })
    return refs


def _supplement_candidate_refs_with_fallback(
    selected_refs: List[Dict[str, Any]],
    *,
    cat_name: str,
    ref_target: int,
    ref_keep_limit: int,
    seen_sc: Set[Tuple[int, int]],
    prefer_serial: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    out = list(selected_refs or [])
    target = max(1, int(ref_target or 1))
    keep_limit = max(target, int(ref_keep_limit or target))
    if len(out) >= target:
        return out, 0
    cat_key = str(cat_name or "").strip()
    if not cat_key:
        return out, 0

    pool_limit = max(target + 8, (target - len(out)) * 4)
    fallback_refs = _fallback_refs_for_cat(
        cat_key,
        limit=pool_limit,
        include_uncropped=False,
        prefer_cached=True,
        prefer_serial=prefer_serial,
    )
    added = 0
    for ref in fallback_refs:
        try:
            serial_ref = int(ref.get("serial"))
            crop_ref = int(ref.get("crop"))
        except Exception:
            continue
        if serial_ref <= 0 or crop_ref <= 0:
            continue
        if _is_flagged_ref_serial(int(serial_ref)):
            continue
        key_sc = (serial_ref, crop_ref)
        if key_sc in seen_sc:
            continue
        entry = _photo_crop_index_cache.get((int(serial_ref), int(crop_ref))) or {}
        if not entry:
            continue
        seen_sc.add(key_sc)
        out.append({
            "img": "",
            "url": _photo_ref_crop_url(serial_ref, crop_ref),
            "serial": serial_ref,
            "crop": crop_ref,
            "source": "metadata_fallback",
        })
        added += 1
        if len(out) >= target or len(out) >= keep_limit:
            break
    return out, added


def _thumb_b64_from_crop(crop: Image.Image, size: int = 128) -> Optional[str]:
    try:
        out = crop.copy()
        out.thumbnail((int(size), int(size)))
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None



def _enrich_manual_candidates(
    candidates: List[Dict[str, Any]],
    alias_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    lookup = alias_lookup or _load_profile_catalog()[0]
    for cand in candidates:
        row = dict(cand or {})
        if isinstance(row.get("refs"), list):
            row["refs"] = _filter_refs_for_flagged_serials(row.get("refs"))
        name = str(row.get("name") or "").strip()
        key = _norm_cat_lookup_token(name)
        meta = _alias_lookup_meta(lookup, key) or {}
        row["cat_id"] = meta.get("cat_id")
        row["display_name"] = str(meta.get("display_name") or name)
        if meta.get("desc") and not row.get("desc"):
            row["desc"] = str(meta.get("desc"))
        row["profile_key"] = str(meta.get("key") or key)
        out.append(row)
    return out


def _manual_ref_cache_status_payload(
    *,
    total_hint: int = 0,
) -> Dict[str, Any]:
    total = max(0, int(total_hint or 0))
    if total <= 0:
        try:
            total = len(_load_profile_catalog()[1])
        except Exception:
            total = max(len(_manual_metadata_ref_cache), len(_photo_crop_index_cache))
    built = int(len(_manual_metadata_ref_cache or {}))
    ready = bool(_manual_metadata_ref_cache) and bool(_photo_crop_index_cache)
    return {
        "ready": bool(ready),
        "building": False,
        "cats": int(total),
        "built": int(total if ready else max(0, built)),
        "total": int(total),
        "per_cat": int(_MANUAL_FALLBACK_REFS_PER_CAT),
        "query_refs_per_cat": int(_MANUAL_QUERY_REFS_PER_CAT),
        "query_ref_cat_limit": int(_MANUAL_QUERY_REF_CAT_LIMIT),
    }


def _ref_crop_cache_key(serial: int, crop_num: int, thumb_size: int) -> str:
    return _hash_cache_key("ref_crop", int(serial), int(crop_num), int(thumb_size))


def _remember_ref_crop_cache_key(serial: int, key: str) -> None:
    """Track which rendered ref crops belong to a serial, for targeted eviction."""
    try:
        sn = int(serial)
    except Exception:
        return
    if sn <= 0:
        return
    _ref_crop_cache_keys_by_serial.setdefault(sn, set()).add(str(key))
    if len(_ref_crop_cache_keys_by_serial) <= _REF_CROP_SERIAL_INDEX_MAX:
        return
    #Drop bookkeeping for serials whose renders have already aged out of the
    #byte cache; the index is only a hint, so losing entries is harmless.
    for tracked_sn, keys in list(_ref_crop_cache_keys_by_serial.items()):
        live = {k for k in keys if k in _ref_crop_result_cache}
        if live:
            _ref_crop_cache_keys_by_serial[tracked_sn] = live
        else:
            _ref_crop_cache_keys_by_serial.pop(tracked_sn, None)


def _drop_ref_crop_renders_for_serials(serials: Optional[List[int]] = None) -> int:
    """Evict rendered ref crops for specific serials, leaving the rest cached."""
    dropped = 0
    for item in serials or []:
        try:
            sn = int(item)
        except Exception:
            continue
        for key in _ref_crop_cache_keys_by_serial.pop(sn, set()):
            if _ref_crop_result_cache.pop(key, None) is not None:
                dropped += 1
    return dropped


def _append_ref_crop_pair(
    out: List[Tuple[int, int]],
    seen: Set[Tuple[int, int]],
    ref: Any,
) -> None:
    if not isinstance(ref, dict):
        return
    try:
        serial_ref = int(ref.get("serial"))
        crop_ref = int(ref.get("crop"))
    except Exception:
        return
    if serial_ref <= 0 or crop_ref <= 0:
        return
    if _is_flagged_ref_serial(int(serial_ref)):
        return
    key = (int(serial_ref), int(crop_ref))
    if key in seen:
        return
    seen.add(key)
    out.append(key)


def _collect_manual_candidate_ref_pairs(
    candidates: List[Dict[str, Any]],
    *,
    max_candidates: int,
    max_refs_per_candidate: int,
) -> List[Tuple[int, int]]:
    rows = list(candidates or [])
    out: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()
    for cand in rows[: max(1, int(max_candidates or 1))]:
        refs = cand.get("refs") if isinstance(cand, dict) else []
        for ref in list(refs or [])[: max(1, int(max_refs_per_candidate or 1))]:
            _append_ref_crop_pair(out, seen, ref)
    return out


def _collect_identify_result_ref_pairs(
    results: List[Dict[str, Any]],
    *,
    max_candidates: int,
    max_refs_per_candidate: int,
) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()
    for crop in list(results or []):
        candidates = crop.get("candidates") if isinstance(crop, dict) else []
        for cand in list(candidates or [])[: max(1, int(max_candidates or 1))]:
            refs = cand.get("refs") if isinstance(cand, dict) else []
            for ref in list(refs or [])[: max(1, int(max_refs_per_candidate or 1))]:
                _append_ref_crop_pair(out, seen, ref)
    return out


def _collect_classifier_ref_cache_pairs(
    *,
    refs_per_cat: int,
) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()
    cache = getattr(V, "_labeler_ref_cache", {}) or {}
    per_cat = max(1, int(refs_per_cat or 1))
    for pack in cache.values():
        refs = pack.get("refs") if isinstance(pack, dict) else []
        for ref in list(refs or [])[:per_cat]:
            _append_ref_crop_pair(out, seen, ref)
    return out


def _collect_manual_metadata_ref_pairs(
    *,
    refs_per_cat: int,
) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()
    per_cat = max(1, int(refs_per_cat or 1))
    for cat_key in list(_manual_metadata_ref_cache.keys()):
        refs = _fallback_refs_for_cat(
            str(cat_key or "").strip(),
            limit=per_cat,
            include_uncropped=False,
            prefer_cached=True,
        )
        for ref in refs:
            _append_ref_crop_pair(out, seen, ref)
    return out


async def _warm_single_ref_crop(
    serial: int,
    crop_num: int,
    *,
    thumb_size: int,
    force: bool = False,
    generation: int = 0,
) -> str:
    # generation > 0 means this came from a scheduled warm batch.  Bail out
    # early if the generation has fallen too far behind the current head so we
    # don't waste a semaphore slot on work the user has already moved past.
    if generation > 0 and (_warm_generation - generation) > _WARM_GENERATION_GRACE:
        return "stale"

    cache_key = _ref_crop_cache_key(int(serial), int(crop_num), int(thumb_size))
    if not force and _cache_get_bytes(
        _ref_crop_result_cache,
        cache_key,
        ttl_sec=_REF_CROP_RESULT_TTL_SEC,
    ):
        return "cached"

    neg_key = (int(serial), int(crop_num))
    bad_until = _ref_crop_negative_cache.get(neg_key, 0.0)
    if bad_until and time.monotonic() < float(bad_until):
        return "negative"

    entry = _photo_crop_index_cache.get((int(serial), int(crop_num)))
    if not entry:
        return "missing_entry"

    box = _parse_yolo_box_str(str(entry.get("box") or "").strip())
    if box is None:
        return "missing_box"

    # Re-check before the potentially long image fetch.
    if generation > 0 and (_warm_generation - generation) > _WARM_GENERATION_GRACE:
        return "stale"

    image_bytes = await labeler_cache.get_cached_image_async(int(serial))
    if not image_bytes:
        image_bytes = await _fetch_image_bytes_for_labeler(
            int(serial),
            str(entry.get("url") or ""),
            bypass_backoff=True,
        )
    if not image_bytes:
        return "missing_image"

    acquired = False
    try:
        # Check right before the semaphore wait — this is the main queue point
        # where stale tasks pile up.
        if generation > 0 and (_warm_generation - generation) > _WARM_GENERATION_GRACE:
            return "stale"
        await _ref_crop_warm_render_sem.acquire()
        acquired = True
        # Check once more after acquiring — we may have waited a while.
        if generation > 0 and (_warm_generation - generation) > _WARM_GENERATION_GRACE:
            return "stale"
        payload, crop_err, _crop_detail = await asyncio.to_thread(
            _render_ref_crop_jpeg,
            image_bytes,
            box,
            int(thumb_size),
            float(settings.cv_pad_pct),
        )
    finally:
        if acquired:
            _ref_crop_warm_render_sem.release()

    if not payload:
        if str(crop_err or "") == "invalid_bounds":
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 600.0
            return "invalid_bounds"
        return "render_failed"

    _cache_set_bytes(
        _ref_crop_result_cache,
        cache_key,
        payload,
        max_items=_REF_CROP_RESULT_CACHE_MAX,
        ttl_sec=_REF_CROP_RESULT_TTL_SEC,
    )
    _remember_ref_crop_cache_key(int(serial), cache_key)
    return "rendered"


async def _warm_ref_crop_pairs(
    pairs: List[Tuple[int, int]],
    *,
    thumb_size: int,
    force: bool = False,
    generation: int = 0,
) -> Dict[str, int]:
    ordered: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()
    for pair in list(pairs or []):
        try:
            serial_ref = int(pair[0])
            crop_ref = int(pair[1])
        except Exception:
            continue
        key = (serial_ref, crop_ref)
        if serial_ref <= 0 or crop_ref <= 0 or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    if not ordered:
        return {"total": 0, "cached": 0, "rendered": 0, "failed": 0, "stale": 0}

    await _ensure_photo_crop_index_cache(force=False)
    work_sem = asyncio.Semaphore(_REF_CROP_WARM_CONCURRENCY)
    counts = {"total": len(ordered), "cached": 0, "rendered": 0, "failed": 0, "stale": 0}

    async def _one(pair: Tuple[int, int]) -> None:
        # Fast exit before even waiting on the internal work semaphore.
        if generation > 0 and (_warm_generation - generation) > _WARM_GENERATION_GRACE:
            counts["stale"] += 1
            return
        async with work_sem:
            status = await _warm_single_ref_crop(
                int(pair[0]),
                int(pair[1]),
                thumb_size=int(thumb_size),
                force=force,
                generation=generation,
            )
        if status == "stale":
            counts["stale"] += 1
        elif status == "cached":
            counts["cached"] += 1
        elif status == "rendered":
            counts["rendered"] += 1
        else:
            counts["failed"] += 1

    await asyncio.gather(*[_one(pair) for pair in ordered], return_exceptions=False)
    return counts


def _schedule_ref_crop_warm(
    pairs: List[Tuple[int, int]],
    *,
    thumb_size: int = _REF_CROP_WARM_SIZE,
    force: bool = False,
) -> int:
    global _warm_generation
    _warm_generation += 1
    gen = _warm_generation

    queued: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()
    for pair in list(pairs or []):
        try:
            serial_ref = int(pair[0])
            crop_ref = int(pair[1])
        except Exception:
            continue
        key = (serial_ref, crop_ref)
        if serial_ref <= 0 or crop_ref <= 0 or key in seen:
            continue
        seen.add(key)
        if not force and _cache_get_bytes(
            _ref_crop_result_cache,
            _ref_crop_cache_key(serial_ref, crop_ref, int(thumb_size)),
            ttl_sec=_REF_CROP_RESULT_TTL_SEC,
        ):
            continue
        queued.append(key)
    if not queued:
        return 0

    async def _runner() -> None:
        try:
            await _warm_ref_crop_pairs(
                queued,
                thumb_size=int(thumb_size),
                force=force,
                generation=gen,
            )
        except Exception:
            pass

    try:
        asyncio.create_task(_runner())
    except Exception:
        return 0
    return len(queued)


def _schedule_classifier_ref_crop_warm(
    *,
    refs_per_cat: int,
    thumb_size: int = _REF_CROP_WARM_SIZE,
    force: bool = False,
) -> bool:
    global _warm_generation
    _warm_generation += 1
    gen = _warm_generation

    async def _runner() -> None:
        force_local = bool(force)
        deadline = time.monotonic() + 120.0
        while True:
            try:
                await V.warm_labeler_refs(force=force_local)
            except Exception:
                return
            force_local = False
            status = V.labeler_ref_status()
            if bool(status.get("ready")):
                break
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.25)
        pairs = _collect_classifier_ref_cache_pairs(
            refs_per_cat=refs_per_cat,
        )
        if not pairs:
            return
        await _warm_ref_crop_pairs(
            pairs,
            thumb_size=int(thumb_size),
            force=force,
            generation=gen,
        )

    try:
        asyncio.create_task(_runner())
    except Exception:
        return False
    return True


def _normalize_manual_candidate_refs(
    refs: List[Dict[str, Any]],
    *,
    cat_name: str,
    prefer_serial: Optional[int] = None,
) -> List[Dict[str, Any]]:
    selected_refs: List[Dict[str, Any]] = []
    seen_sc: Set[Tuple[int, int]] = set()
    keep_limit = max(1, int(_MANUAL_FALLBACK_REFS_PER_CAT))
    for ref in list(refs or []):
        if not isinstance(ref, dict):
            continue
        try:
            serial_ref = int(ref.get("serial"))
            crop_ref = int(ref.get("crop"))
        except Exception:
            continue
        if serial_ref <= 0 or crop_ref <= 0:
            continue
        if _is_flagged_ref_serial(int(serial_ref)):
            continue
        key_sc = (int(serial_ref), int(crop_ref))
        if key_sc in seen_sc:
            continue
        entry = _photo_crop_index_cache.get(key_sc) or {}
        if not entry:
            continue
        seen_sc.add(key_sc)
        selected_refs.append({
            "img": "",
            "url": _photo_ref_crop_url(int(serial_ref), int(crop_ref)),
            "serial": int(serial_ref),
            "crop": int(crop_ref),
            "source": str(ref.get("source") or "manual_query"),
        })
        if len(selected_refs) >= keep_limit:
            break
    selected_refs, _ = _supplement_candidate_refs_with_fallback(
        selected_refs,
        cat_name=str(cat_name or "").strip(),
        ref_target=int(_MANUAL_FALLBACK_REFS_PER_CAT),
        ref_keep_limit=keep_limit,
        seen_sc=seen_sc,
        prefer_serial=prefer_serial,
    )
    if selected_refs:
        return selected_refs[:keep_limit]
    return _fallback_refs_for_cat(
        str(cat_name or "").strip(),
        limit=keep_limit,
        prefer_cached=True,
        prefer_serial=prefer_serial,
    )


def _build_manual_candidate_catalog(
    raw_candidates: List[Dict[str, Any]],
    *,
    alias_lookup: Dict[str, Dict[str, Any]],
    ordered_profile: List[Dict[str, Any]],
    prefer_serial: Optional[int] = None,
) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    ranked_keys: List[str] = []
    for cand in _enrich_manual_candidates(raw_candidates or [], alias_lookup):
        row = dict(cand or {})
        pkey = str(row.pop("profile_key", "")).strip()
        if not pkey:
            pkey = _norm_cat_lookup_token(str(row.get("name") or ""))
        if not pkey or pkey in by_key:
            continue
        row["refs"] = _normalize_manual_candidate_refs(
            list(row.get("refs") or []),
            cat_name=pkey,
            prefer_serial=prefer_serial,
        )
        by_key[pkey] = row
        ranked_keys.append(pkey)

    def _safe_cat_id(val: Any) -> int:
        try:
            if val is None or str(val).strip() == "":
                return 10**9
            return int(val)
        except Exception:
            return 10**9

    tail_keys: List[str] = []
    for meta in ordered_profile or []:
        key = str(meta.get("key") or "").strip()
        if not key:
            continue
        existing = by_key.get(key)
        if existing:
            if not existing.get("display_name"):
                existing["display_name"] = str(meta.get("display_name") or existing.get("name") or "")
            if existing.get("cat_id") is None:
                existing["cat_id"] = meta.get("cat_id")
            if not existing.get("desc") and meta.get("desc"):
                existing["desc"] = str(meta.get("desc"))
            if not existing.get("refs"):
                existing["refs"] = _fallback_refs_for_cat(
                    key,
                    limit=_MANUAL_FALLBACK_REFS_PER_CAT,
                    prefer_cached=True,
                    prefer_serial=prefer_serial,
                )
            continue
        by_key[key] = {
            "name": str(meta.get("name") or ""),
            "display_name": str(meta.get("display_name") or meta.get("name") or ""),
            "cat_id": meta.get("cat_id"),
            "desc": str(meta.get("desc") or ""),
            "conf": None,
            "refs": _fallback_refs_for_cat(
                key,
                limit=_MANUAL_FALLBACK_REFS_PER_CAT,
                prefer_cached=True,
                prefer_serial=prefer_serial,
            ),
        }
        tail_keys.append(key)

    tail_keys.sort(
        key=lambda key: (
            _safe_cat_id((by_key.get(key) or {}).get("cat_id")),
            str((by_key.get(key) or {}).get("display_name") or (by_key.get(key) or {}).get("name") or ""),
        )
    )
    out: List[Dict[str, Any]] = []
    for key in ranked_keys + tail_keys:
        row = by_key.get(key)
        if not row:
            continue
        out.append(row)
    return out


def _purge_expired_claims(now_mono: float) -> None:
    expired = [
        key
        for key, claim in _active_claims.items()
        if float(claim.get("expires_at") or 0.0) <= now_mono
    ]
    for key in expired:
        _active_claims.pop(key, None)


async def _claims_snapshot() -> Dict[Tuple[str, int], Dict[str, Any]]:
    now_mono = time.monotonic()
    async with _claim_lock:
        _purge_expired_claims(now_mono)
        return {k: dict(v) for k, v in _active_claims.items()}


async def _acquire_claim(mode: str, serial: int, user_id: str, username: str) -> Tuple[bool, Dict[str, Any]]:
    now_mono = time.monotonic()
    key = (mode, int(serial))
    async with _claim_lock:
        _purge_expired_claims(now_mono)
        current = _active_claims.get(key)
        if current and str(current.get("user_id") or "") != str(user_id or ""):
            return False, dict(current)
        claim = {
            "user_id": str(user_id or ""),
            "username": str(username or ""),
            "expires_at": now_mono + float(_LABELER_CLAIM_TTL_SEC),
        }
        _active_claims[key] = claim
        return True, dict(claim)


async def _release_claim(mode: str, serial: int, user_id: str) -> bool:
    key = (mode, int(serial))
    async with _claim_lock:
        current = _active_claims.get(key)
        if not current:
            return False
        if str(current.get("user_id") or "") != str(user_id or ""):
            return False
        _active_claims.pop(key, None)
        return True


def _with_cors(resp: web.Response, request: web.Request) -> web.Response:
    """Add CORS headers to response using the configured UI allowlist."""
    origin = request.headers.get("Origin")
    allow_origin = None
    if origin and ("*" in _LABELER_ALLOWED_ORIGINS or origin in _LABELER_ALLOWED_ORIGINS):
        allow_origin = origin
    elif "*" in _LABELER_ALLOWED_ORIGINS:
        allow_origin = "*"

    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-CSRF-Token, X-TC-CSRF"
    resp.headers["Vary"] = "Origin"
    if allow_origin:
        resp.headers["Access-Control-Allow-Origin"] = allow_origin
    if allow_origin and allow_origin != "*":
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp


def _internal_error_response(
    request: web.Request,
    *,
    message: str = "Internal server error",
) -> web.Response:
    """Return a generic 500 without exposing exception details to the client."""
    return _with_cors(web.Response(status=500, text=message), request)


def _open_rgb_image(source: Any) -> Image.Image:
    """Open an image and normalize EXIF orientation before RGB conversion."""
    img = Image.open(source)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img.convert("RGB")


def _render_ref_crop_jpeg(
    image_bytes: bytes,
    box: Tuple[float, float, float, float],
    thumb_size: int,
    pad: float,
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Decode, crop, and encode a reference JPEG crop in a worker thread."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    img_w, img_h = img.size
    cx, cy, w, h = box
    x1 = (cx - w / 2) * img_w
    y1 = (cy - h / 2) * img_h
    x2 = (cx + w / 2) * img_w
    y2 = (cy + h / 2) * img_h
    bw = x2 - x1
    bh = y2 - y1
    px = bw * float(pad)
    py = bh * float(pad)
    cx1 = max(0, int(round(x1 - px)))
    cy1 = max(0, int(round(y1 - py)))
    cx2 = min(int(img_w), int(round(x2 + px)))
    cy2 = min(int(img_h), int(round(y2 + py)))
    if cx2 <= cx1 or cy2 <= cy1:
        return None, "invalid_bounds", f"img={int(img_w)}x{int(img_h)}"

    crop = img.crop((cx1, cy1, cx2, cy2))
    crop.thumbnail((int(thumb_size), int(thumb_size)))
    out = io.BytesIO()
    crop.save(out, format="JPEG", quality=86)
    return out.getvalue(), None, None


def _vision_working_image_size(image_bytes: bytes) -> Tuple[int, int]:
    """Return the effective image size used by vision.detect/refine preprocessing."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    max_dim = int(getattr(settings, "cv_max_image_dim", 0) or 0)
    if max_dim > 0:
        w, h = img.size
        if w > max_dim or h > max_dim:
            if w > h:
                target = (int(max_dim), max(1, int(h * (float(max_dim) / float(w)))))
            else:
                target = (max(1, int(w * (float(max_dim) / float(h)))), int(max_dim))
            try:
                # Mirror tomcat.vision._enforce_max_dim() behavior (JPEG decoder draft mode).
                img.draft(None, target)
            except Exception:
                pass
    return img.size


def _boxes_to_yolo_strings(boxes: List[Tuple[float, float, float, float]]) -> List[str]:
    out: List[str] = []
    for box in boxes:
        try:
            cx, cy, w, h = [float(x) for x in box]
        except Exception:
            continue
        out.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return out


def _normalize_abs_polygons(
    polygons: List[List[Tuple[float, float]]],
    img_w: int,
    img_h: int,
) -> List[List[List[float]]]:
    out: List[List[List[float]]] = []
    w = max(1, int(img_w or 0))
    h = max(1, int(img_h or 0))
    for poly in list(polygons or []):
        pts: List[List[float]] = []
        for pt in list(poly or []):
            try:
                x, y = [float(v) for v in pt]
            except Exception:
                continue
            pts.append([
                round(max(0.0, min(1.0, x / float(w))), 5),
                round(max(0.0, min(1.0, y / float(h))), 5),
            ])
        out.append(pts)
    return out


def _normalize_abs_mask_tiles(
    tiles: List[Dict[str, Any]],
    img_w: int,
    img_h: int,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    w = max(1, int(img_w or 0))
    h = max(1, int(img_h or 0))
    for tile in list(tiles or []):
        row = dict(tile or {})
        png_b64 = str(row.get("png_b64") or "").strip()
        if not png_b64:
            out.append({})
            continue
        try:
            x1 = float(row.get("x1"))
            y1 = float(row.get("y1"))
            x2 = float(row.get("x2"))
            y2 = float(row.get("y2"))
        except Exception:
            out.append({})
            continue
        out.append({
            "x1": round(max(0.0, min(1.0, x1 / float(w))), 5),
            "y1": round(max(0.0, min(1.0, y1 / float(h))), 5),
            "x2": round(max(0.0, min(1.0, x2 / float(w))), 5),
            "y2": round(max(0.0, min(1.0, y2 / float(h))), 5),
            "png_b64": png_b64,
        })
    return out


def _box_area_xyxy(box: Tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = [float(v) for v in box]
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _box_delta_summary(
    before: List[Tuple[float, float, float, float]],
    after: List[Tuple[float, float, float, float]],
) -> Dict[str, Any]:
    pair_count = min(len(before), len(after))
    shifted = 0
    max_center_shift_px = 0.0
    max_edge_shift_px = 0.0
    max_area_ratio = 1.0
    for idx in range(pair_count):
        bx1, by1, bx2, by2 = [float(v) for v in before[idx]]
        ax1, ay1, ax2, ay2 = [float(v) for v in after[idx]]
        bcx = (bx1 + bx2) / 2.0
        bcy = (by1 + by2) / 2.0
        acx = (ax1 + ax2) / 2.0
        acy = (ay1 + ay2) / 2.0
        center_shift = ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5
        edge_shift = max(
            abs(ax1 - bx1),
            abs(ay1 - by1),
            abs(ax2 - bx2),
            abs(ay2 - by2),
        )
        before_area = _box_area_xyxy(before[idx])
        after_area = _box_area_xyxy(after[idx])
        area_ratio = (after_area / before_area) if before_area > 0 else 1.0
        if center_shift >= 1.0 or edge_shift >= 1.0 or abs(area_ratio - 1.0) >= 0.02:
            shifted += 1
        max_center_shift_px = max(max_center_shift_px, center_shift)
        max_edge_shift_px = max(max_edge_shift_px, edge_shift)
        max_area_ratio = max(max_area_ratio, area_ratio)
    return {
        "pairs": int(pair_count),
        "shifted": int(shifted),
        "max_center_shift_px": round(float(max_center_shift_px), 2),
        "max_edge_shift_px": round(float(max_edge_shift_px), 2),
        "max_area_ratio": round(float(max_area_ratio), 3),
    }


def _compact_refine_summary(summary: Optional[Dict[str, Any]]) -> str:
    if not isinstance(summary, dict):
        return "sam=none"
    selected = summary.get("selected") if isinstance(summary.get("selected"), dict) else {}
    sample_bits: List[str] = []
    for sample in list(summary.get("samples") or [])[:2]:
        if not isinstance(sample, dict):
            continue
        bit = (
            f"b{int(sample.get('box_index') or 0)}:"
            f"{str(sample.get('selected') or 'fallback')}/"
            f"{str(sample.get('reason') or 'ok')}"
            f"[cand={int(sample.get('candidate_masks') or 0)}"
            f",acc={int(sample.get('accepted_masks') or 0)}"
            f",rej={int(sample.get('guard_rejections') or 0)}"
        )
        if str(sample.get("selected") or "") == "fallback":
            bit += (
                f",prev_iou={float(sample.get('preview_iou') or 0.0):.3f}"
                f",prev_cover={float(sample.get('preview_detector_coverage') or 0.0):.3f}"
                f",prev_mask={float(sample.get('preview_detector_mask_ratio') or 0.0):.3f}"
                f",prev_area={float(sample.get('preview_area_ratio') or 1.0):.3f}"
            )
        bit += "]"
        sample_bits.append(bit)
    sample_txt = f"; samples={'|'.join(sample_bits)}" if sample_bits else ""
    return (
        f"boxes={int(summary.get('boxes') or 0)}; "
        f"accepted={int(summary.get('accepted_boxes') or 0)}; "
        f"fallback={int(summary.get('fallback_boxes') or 0)}; "
        f"clipped={int(summary.get('clipped_boxes') or 0)}; "
        f"guard_reject_boxes={int(summary.get('guard_reject_boxes') or 0)}; "
        f"candidate_masks={int(summary.get('candidate_masks') or 0)}; "
        f"accepted_masks={int(summary.get('accepted_masks') or 0)}; "
        f"selected_tight={int(selected.get('tight') or 0)}; "
        f"selected_iou={int(selected.get('iou') or 0)}; "
        f"max_outside={float(summary.get('max_outside_guard_ratio') or 0.0):.3f}; "
        f"max_cover={float(summary.get('max_detector_coverage') or 0.0):.3f}; "
        f"max_area_ratio={float(summary.get('max_area_ratio') or 1.0):.3f}"
        f"{sample_txt}"
    )


def _compact_exception_diag(exc: BaseException, *, max_len: int = 160) -> Tuple[str, str]:
    kind = str(type(exc).__name__ or "Exception")
    text = str(exc or "").strip() or repr(exc)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 3].rstrip() + "..."
    return kind, text


def _hash_cache_key(*parts: Any) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def _kickoff_photo_metadata_cache_refresh(reason: str = "") -> None:
    """Refresh the local photo metadata cache in a worker thread."""
    global _photo_metadata_cache_refresh_task, _photo_metadata_cache_refresh_next_allowed_mono
    now_mono = time.monotonic()
    active = _photo_metadata_cache_refresh_task
    if active is not None and not active.done():
        return
    if now_mono < float(_photo_metadata_cache_refresh_next_allowed_mono):
        return
    _photo_metadata_cache_refresh_next_allowed_mono = now_mono + float(_PHOTO_METADATA_CACHE_REFRESH_COOLDOWN_SEC)

    async def _run() -> None:
        global _photo_metadata_cache_refresh_task
        try:
            await asyncio.to_thread(force_refresh_photo_rows_cache)
        except Exception as e:
            log_action(
                "labeler_photo_metadata_cache_refresh_error",
                f"reason={str(reason or '').strip() or 'unknown'}",
                f"{type(e).__name__}: {e!r}",
            )
        finally:
            _photo_metadata_cache_refresh_task = None

    try:
        _photo_metadata_cache_refresh_task = asyncio.create_task(_run())
    except Exception:
        _photo_metadata_cache_refresh_task = None


def _identify_should_trace(prefetch: bool) -> bool:
    if not _IDENTIFY_DEBUG:
        return False
    if not prefetch:
        return True
    return random.random() < _IDENTIFY_DEBUG_PREFETCH_SAMPLE


def _identify_result_ref_stats(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    crops = 0
    cands = 0
    with_refs = 0
    zero_refs = 0
    total_refs = 0
    max_refs = 0
    inline_refs = 0
    url_refs = 0
    inline_cands = 0
    url_only_cands = 0
    for crop in results or []:
        candidates = crop.get("candidates", []) if isinstance(crop, dict) else []
        if not isinstance(candidates, list):
            continue
        crops += 1
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            refs = cand.get("refs")
            ref_count = len(refs) if isinstance(refs, list) else 0
            cands += 1
            total_refs += ref_count
            if ref_count > 0:
                with_refs += 1
            else:
                zero_refs += 1
            cand_inline = 0
            cand_url = 0
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, str):
                    r = str(ref).strip()
                    if r:
                        cand_url += 1
                    continue
                if not isinstance(ref, dict):
                    continue
                if str(ref.get("img") or ref.get("thumb") or "").strip():
                    cand_inline += 1
                elif str(ref.get("url") or ref.get("src") or "").strip():
                    cand_url += 1
            inline_refs += cand_inline
            url_refs += cand_url
            if cand_inline > 0:
                inline_cands += 1
            elif cand_url > 0:
                url_only_cands += 1
            if ref_count > max_refs:
                max_refs = ref_count
    avg_refs = (float(total_refs) / float(cands)) if cands > 0 else 0.0
    return {
        "crops": crops,
        "cands": cands,
        "with_refs": with_refs,
        "zero_refs": zero_refs,
        "avg_refs": round(avg_refs, 2),
        "max_refs": max_refs,
        "inline_refs": inline_refs,
        "url_refs": url_refs,
        "inline_cands": inline_cands,
        "url_only_cands": url_only_cands,
    }


def _cache_get(cache: Dict[str, Tuple[float, Dict[str, Any]]], key: str) -> Optional[Dict[str, Any]]:
    rec = cache.get(str(key))
    if not rec:
        return None
    ts, payload = rec
    if (time.monotonic() - float(ts)) > _CV_RESULT_TTL_SEC:
        cache.pop(str(key), None)
        return None
    return dict(payload)


def _cache_set(
    cache: Dict[str, Tuple[float, Dict[str, Any]]],
    key: str,
    payload: Dict[str, Any],
    *,
    max_items: int,
) -> None:
    now = time.monotonic()
    cache[str(key)] = (now, dict(payload))

    # Prune expired entries first.
    expiry = now - _CV_RESULT_TTL_SEC
    for k, (ts, _) in list(cache.items()):
        if float(ts) < expiry:
            cache.pop(k, None)

    if len(cache) <= max_items:
        return

    overflow = len(cache) - int(max_items)
    if overflow <= 0:
        return
    oldest = sorted(cache.items(), key=lambda kv: float(kv[1][0]))[:overflow]
    for k, _ in oldest:
        cache.pop(k, None)


def _cache_get_bytes(
    cache: Dict[str, Tuple[float, bytes]],
    key: str,
    *,
    ttl_sec: Optional[float] = None,
) -> Optional[bytes]:
    rec = cache.get(str(key))
    if not rec:
        return None
    ts, payload = rec
    ttl = max(1.0, float(_CV_RESULT_TTL_SEC if ttl_sec is None else ttl_sec))
    if (time.monotonic() - float(ts)) > ttl:
        cache.pop(str(key), None)
        return None
    return bytes(payload)


def _cache_set_bytes(
    cache: Dict[str, Tuple[float, bytes]],
    key: str,
    payload: bytes,
    *,
    max_items: int,
    ttl_sec: Optional[float] = None,
) -> None:
    now = time.monotonic()
    cache[str(key)] = (now, bytes(payload))

    ttl = max(1.0, float(_CV_RESULT_TTL_SEC if ttl_sec is None else ttl_sec))
    expiry = now - ttl
    for k, (ts, _) in list(cache.items()):
        if float(ts) < expiry:
            cache.pop(k, None)

    if len(cache) <= max_items:
        return

    overflow = len(cache) - int(max_items)
    if overflow <= 0:
        return
    oldest = sorted(cache.items(), key=lambda kv: float(kv[1][0]))[:overflow]
    for k, _ in oldest:
        cache.pop(k, None)


def _log_ref_crop_miss(sn: int, crop_num: int, reason: str, extra: str = "") -> None:
    """Throttle repetitive ref-crop miss logs for the same serial/crop/reason."""
    global _ref_crop_img_unavail_window_start_mono
    global _ref_crop_img_unavail_logged, _ref_crop_img_unavail_suppressed
    reason_norm = str(reason or "").strip().lower()
    now = time.monotonic()
    if reason_norm == "image_unavailable" and not _LOG_REF_CROP_IMAGE_UNAVAILABLE_MISS:
        return

    # Global cap for image_unavailable noise bursts across many different refs.
    if reason_norm == "image_unavailable":
        if _ref_crop_img_unavail_window_start_mono <= 0.0:
            _ref_crop_img_unavail_window_start_mono = now
        elapsed = now - float(_ref_crop_img_unavail_window_start_mono)
        if elapsed >= float(_REF_CROP_IMAGE_UNAVAILABLE_WINDOW_SEC):
            if _ref_crop_img_unavail_suppressed > 0:
                log_action(
                    "labeler_ref_crop_miss_suppressed",
                    "reason=image_unavailable",
                    (
                        f"suppressed={int(_ref_crop_img_unavail_suppressed)}; "
                        f"window_s={int(round(elapsed))}"
                    ),
                )
            _ref_crop_img_unavail_window_start_mono = now
            _ref_crop_img_unavail_logged = 0
            _ref_crop_img_unavail_suppressed = 0
        if _ref_crop_img_unavail_logged >= int(_REF_CROP_IMAGE_UNAVAILABLE_MAX_PER_WINDOW):
            _ref_crop_img_unavail_suppressed += 1
            return
        _ref_crop_img_unavail_logged += 1

    key = f"{int(sn)}:{int(crop_num)}:{str(reason or '').strip().lower()}"
    next_allowed = float(_ref_crop_miss_next_log_mono.get(key, 0.0))
    if next_allowed > now:
        _ref_crop_miss_suppressed[key] = int(_ref_crop_miss_suppressed.get(key, 0)) + 1
        return

    suppressed = int(_ref_crop_miss_suppressed.pop(key, 0))
    detail = f"reason={str(reason or '').strip() or 'unknown'}"
    extra_s = str(extra or "").strip()
    if extra_s:
        detail = f"{detail}; {extra_s}"
    if suppressed > 0:
        detail = f"{detail}; suppressed={suppressed}"
    log_action(
        "labeler_ref_crop_miss",
        f"sn={int(sn)}; crop={int(crop_num)}",
        detail,
    )
    _ref_crop_miss_next_log_mono[key] = now + float(_REF_CROP_MISS_LOG_COOLDOWN_SEC)


async def _fetch_image_bytes_for_labeler(
    serial: Optional[int],
    url: Optional[str],
    *,
    bypass_backoff: bool = False,
) -> Optional[bytes]:
    """Best-effort image fetch from local storage and local cache only."""
    del url, bypass_backoff
    data: Optional[bytes] = None
    serial_i = int(serial) if serial is not None else None

    if serial_i is not None:
        try:
            data = await asyncio.to_thread(local_photos.read_local_photo_bytes, int(serial_i))
        except Exception:
            data = None
    if data:
        return data

    if serial_i is not None:
        try:
            data = await labeler_cache.get_cached_image_async(serial_i)
        except Exception:
            data = None
    if data:
        return data
    return None


async def _identify_singleflight_enter(cache_key: str) -> Tuple[asyncio.Future, bool]:
    """Return (future, is_owner) for keyed identify single-flight."""
    key = str(cache_key or "")
    loop = asyncio.get_running_loop()
    async with _identify_inflight_lock:
        existing = _identify_inflight.get(key)
        if existing is not None and not existing.done():
            return existing, False
        fut: asyncio.Future = loop.create_future()
        _identify_inflight[key] = fut
        return fut, True


async def _identify_singleflight_finish(
    cache_key: str,
    fut: asyncio.Future,
    payload: Optional[Dict[str, Any]],
) -> None:
    key = str(cache_key or "")
    async with _identify_inflight_lock:
        current = _identify_inflight.get(key)
        if current is fut:
            _identify_inflight.pop(key, None)
    if fut.done():
        return
    if isinstance(payload, dict):
        fut.set_result(dict(payload))
    else:
        fut.set_result({})


async def _classify_quality_singleflight_enter(serial: int) -> Tuple[asyncio.Future, bool]:
    """Return (future, is_owner) for per-serial classify quality eval single-flight."""
    sn = int(serial)
    loop = asyncio.get_running_loop()
    async with _classify_quality_inflight_lock:
        existing = _classify_quality_inflight.get(sn)
        if existing is not None and not existing.done():
            return existing, False
        fut: asyncio.Future = loop.create_future()
        _classify_quality_inflight[sn] = fut
        return fut, True


async def _classify_quality_singleflight_finish(
    serial: int,
    fut: asyncio.Future,
    result: Optional[Tuple[bool, Dict[str, Any]]] = None,
    error: Optional[BaseException] = None,
) -> None:
    sn = int(serial)
    async with _classify_quality_inflight_lock:
        current = _classify_quality_inflight.get(sn)
        if current is fut:
            _classify_quality_inflight.pop(sn, None)
    if fut.done():
        return
    if error is not None:
        fut.set_exception(error)
        return
    fut.set_result(result if result is not None else (False, {"reasons": ["unknown"], "hard_fail": False}))


def _cache_get_classify_quality(serial: int) -> Optional[Tuple[bool, int, int, float]]:
    rec = _classify_quality_cache.get(int(serial))
    if not rec:
        return None
    ts, ok, w, h, blur = rec
    if (time.monotonic() - float(ts)) > _CLASSIFY_PREFILTER_CACHE_TTL_SEC:
        _classify_quality_cache.pop(int(serial), None)
        return None
    return bool(ok), int(w), int(h), float(blur)


def _cache_get_classify_quality_soft_fail(serial: int) -> Optional[str]:
    rec = _classify_quality_soft_fail_cache.get(int(serial))
    if not rec:
        return None
    ts, reason = rec
    if (time.monotonic() - float(ts)) > _CLASSIFY_PREFILTER_SOFT_FAIL_TTL_SEC:
        _classify_quality_soft_fail_cache.pop(int(serial), None)
        return None
    return str(reason or "").strip().lower() or "fetch"


def _cache_set_classify_quality_soft_fail(serial: int, reason: str) -> None:
    _classify_quality_soft_fail_cache[int(serial)] = (
        time.monotonic(),
        str(reason or "").strip().lower() or "fetch",
    )
    if len(_classify_quality_soft_fail_cache) <= _CLASSIFY_PREFILTER_CACHE_MAX:
        return
    overflow = len(_classify_quality_soft_fail_cache) - int(_CLASSIFY_PREFILTER_CACHE_MAX)
    if overflow <= 0:
        return
    oldest = sorted(
        _classify_quality_soft_fail_cache.items(),
        key=lambda kv: float(kv[1][0]),
    )[:overflow]
    for key, _ in oldest:
        _classify_quality_soft_fail_cache.pop(int(key), None)


def _cache_set_classify_quality(serial: int, ok: bool, width: int, height: int, blur: float) -> None:
    _classify_quality_soft_fail_cache.pop(int(serial), None)
    _classify_quality_cache[int(serial)] = (
        time.monotonic(),
        bool(ok),
        int(width),
        int(height),
        float(blur),
    )
    if len(_classify_quality_cache) <= _CLASSIFY_PREFILTER_CACHE_MAX:
        return
    overflow = len(_classify_quality_cache) - int(_CLASSIFY_PREFILTER_CACHE_MAX)
    if overflow <= 0:
        return
    oldest = sorted(_classify_quality_cache.items(), key=lambda kv: float(kv[1][0]))[:overflow]
    for key, _ in oldest:
        _classify_quality_cache.pop(int(key), None)


def _laplacian_variance(gray_u8: Any) -> float:
    """Best-effort blur score; higher means sharper."""
    try:
        import cv2  # type: ignore
        return float(cv2.Laplacian(gray_u8, cv2.CV_64F).var())
    except Exception:
        pass
    try:
        import numpy as np  # type: ignore
        arr = np.asarray(gray_u8, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] < 3 or arr.shape[1] < 3:
            return 0.0
        center = arr[1:-1, 1:-1]
        lap = (-4.0 * center) + arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
        return float(lap.var())
    except Exception:
        return 0.0


def _compute_blur_score(img: Image.Image) -> float:
    try:
        gray = img.convert("L")
        max_dim = int(_CLASSIFY_BLUR_MAX_DIM or 0)
        if max_dim > 0:
            w, h = gray.size
            if max(w, h) > max_dim:
                ratio = float(max_dim) / float(max(w, h))
                nw = max(1, int(round(w * ratio)))
                nh = max(1, int(round(h * ratio)))
                gray = gray.resize((nw, nh), resample=Image.Resampling.BILINEAR)
        return _laplacian_variance(gray)
    except Exception:
        return 0.0


def _decode_classify_quality_metrics(data: bytes) -> Tuple[int, int, float]:
    """Decode image bytes and compute width/height/blur off the event loop."""
    img = _open_rgb_image(io.BytesIO(data))
    width, height = [int(x) for x in img.size]
    blur = _compute_blur_score(img)
    return int(width), int(height), float(blur)


async def _evaluate_classify_quality_uncached(
    serial: int,
    url: str,
    *,
    source: str = "unknown",
) -> Tuple[bool, Dict[str, Any]]:
    """Evaluate image quality against classify queue gates."""
    data = await _fetch_image_bytes_for_labeler(int(serial), str(url or "").strip())
    if not data:
        _cache_set_classify_quality_soft_fail(int(serial), "fetch")
        return False, {
            "width": 0,
            "height": 0,
            "pixels": 0,
            "blur": 0.0,
            "reasons": ["fetch"],
            "hard_fail": False,
        }

    width = 0
    height = 0
    blur = 0.0
    try:
        width, height, blur = await asyncio.to_thread(_decode_classify_quality_metrics, data)
    except Exception:
        _cache_set_classify_quality_soft_fail(int(serial), "decode")
        return False, {
            "width": 0,
            "height": 0,
            "pixels": 0,
            "blur": 0.0,
            "reasons": ["decode"],
            "hard_fail": False,
        }

    pixels = int(width * height)
    reasons: List[str] = []
    if _CLASSIFY_MIN_PIXELS > 0 and pixels < _CLASSIFY_MIN_PIXELS:
        reasons.append("pixels")
    if _CLASSIFY_MIN_DIM > 0 and (width < _CLASSIFY_MIN_DIM or height < _CLASSIFY_MIN_DIM):
        reasons.append("min_dim")
    if _CLASSIFY_MIN_BLUR > 0 and float(blur) < _CLASSIFY_MIN_BLUR:
        reasons.append("blur")
    ok = not reasons
    _cache_set_classify_quality(int(serial), ok, width, height, blur)
    return ok, {
        "width": int(width),
        "height": int(height),
        "pixels": int(pixels),
        "blur": float(blur),
        "reasons": reasons,
        "hard_fail": bool(reasons),
    }


async def _evaluate_classify_quality(
    serial: int,
    url: str,
    *,
    source: str = "unknown",
) -> Tuple[bool, Dict[str, Any]]:
    """Evaluate image quality against classify queue gates with single-flight dedupe."""
    if _CLASSIFY_MIN_PIXELS <= 0 and _CLASSIFY_MIN_DIM <= 0 and _CLASSIFY_MIN_BLUR <= 0:
        return True, {"width": 0, "height": 0, "pixels": 0, "blur": 0.0, "reasons": []}
    sn = int(serial)
    cached = _cache_get_classify_quality(sn)
    if cached is not None:
        ok, width, height, blur = cached
        pixels = int(width * height)
        reasons: List[str] = []
        if _CLASSIFY_MIN_PIXELS > 0 and pixels < _CLASSIFY_MIN_PIXELS:
            reasons.append("pixels")
        if _CLASSIFY_MIN_DIM > 0 and (width < _CLASSIFY_MIN_DIM or height < _CLASSIFY_MIN_DIM):
            reasons.append("min_dim")
        if _CLASSIFY_MIN_BLUR > 0 and float(blur) < _CLASSIFY_MIN_BLUR:
            reasons.append("blur")
        return bool(ok and not reasons), {
            "width": int(width),
            "height": int(height),
            "pixels": int(pixels),
            "blur": float(blur),
            "reasons": reasons,
            "hard_fail": bool(reasons),
        }
    soft_fail_reason = _cache_get_classify_quality_soft_fail(sn)
    if soft_fail_reason:
        return False, {
            "width": 0,
            "height": 0,
            "pixels": 0,
            "blur": 0.0,
            "reasons": [soft_fail_reason],
            "hard_fail": False,
        }

    fut, is_owner = await _classify_quality_singleflight_enter(sn)
    if not is_owner:
        try:
            shared = await fut
            if isinstance(shared, tuple) and len(shared) == 2:
                return shared  # type: ignore[return-value]
        except Exception:
            pass
        # If the owner lookup fails, try the cached score once more before recomputing.
        cached = _cache_get_classify_quality(sn)
        if cached is not None:
            ok, width, height, blur = cached
            pixels = int(width * height)
            reasons: List[str] = []
            if _CLASSIFY_MIN_PIXELS > 0 and pixels < _CLASSIFY_MIN_PIXELS:
                reasons.append("pixels")
            if _CLASSIFY_MIN_DIM > 0 and (width < _CLASSIFY_MIN_DIM or height < _CLASSIFY_MIN_DIM):
                reasons.append("min_dim")
            if _CLASSIFY_MIN_BLUR > 0 and float(blur) < _CLASSIFY_MIN_BLUR:
                reasons.append("blur")
            return bool(ok and not reasons), {
                "width": int(width),
                "height": int(height),
                "pixels": int(pixels),
                "blur": float(blur),
                "reasons": reasons,
                "hard_fail": bool(reasons),
            }
        soft_fail_reason = _cache_get_classify_quality_soft_fail(sn)
        if soft_fail_reason:
            return False, {
                "width": 0,
                "height": 0,
                "pixels": 0,
                "blur": 0.0,
                "reasons": [soft_fail_reason],
                "hard_fail": False,
            }

    try:
        result = await _evaluate_classify_quality_uncached(sn, str(url or "").strip(), source=source)
    except Exception as e:
        await _classify_quality_singleflight_finish(sn, fut, error=e)
        raise
    await _classify_quality_singleflight_finish(sn, fut, result=result)
    return result


def _evaluate_cached_classify_quality(serial: int) -> Optional[Tuple[bool, Dict[str, Any]]]:
    cached = _cache_get_classify_quality(int(serial))
    if cached is None:
        soft_fail_reason = _cache_get_classify_quality_soft_fail(int(serial))
        if not soft_fail_reason:
            return None
        return False, {
            "width": 0,
            "height": 0,
            "pixels": 0,
            "blur": 0.0,
            "reasons": [soft_fail_reason],
            "hard_fail": False,
        }
    ok, width, height, blur = cached
    pixels = int(width * height)
    reasons: List[str] = []
    if _CLASSIFY_MIN_PIXELS > 0 and pixels < _CLASSIFY_MIN_PIXELS:
        reasons.append("pixels")
    if _CLASSIFY_MIN_DIM > 0 and (width < _CLASSIFY_MIN_DIM or height < _CLASSIFY_MIN_DIM):
        reasons.append("min_dim")
    if _CLASSIFY_MIN_BLUR > 0 and float(blur) < _CLASSIFY_MIN_BLUR:
        reasons.append("blur")
    passes = bool(ok and not reasons)
    return passes, {
        "width": int(width),
        "height": int(height),
        "pixels": int(pixels),
        "blur": float(blur),
        "reasons": reasons,
        "hard_fail": bool(reasons),
    }


def _build_rejected_labels(num_boxes: int) -> str:
    n = max(1, int(num_boxes or 0))
    return "|".join(["Rejected"] * n)


def _schedule_classify_quality_scan_from_queue(items: List[Dict[str, Any]]) -> int:
    """Throttle/cap queue-triggered quality scans so active labeling stays responsive."""
    global _classify_quality_bg_queue_scan_next_mono
    if not items:
        return 0
    if float(_loop_lag_ms) >= float(_LOOP_LAG_SHED_BG_WORK_MS):
        return 0
    cap = int(_CLASSIFY_PREFILTER_BG_QUEUE_SCAN_MAX_ITEMS or 0)
    if cap <= 0:
        return 0
    now = time.monotonic()
    if now < float(_classify_quality_bg_queue_scan_next_mono):
        return 0
    _classify_quality_bg_queue_scan_next_mono = now + float(_CLASSIFY_PREFILTER_BG_QUEUE_SCAN_COOLDOWN_SEC)
    selected = items[:cap]
    if not selected:
        return 0
    _schedule_classify_quality_scan(selected)
    return len(selected)


def _auto_reject_low_quality_sync(items: List[Dict[str, Any]]) -> int:
    """Mark low-quality classify rows as Rejected in the local metadata CSV."""
    if not items:
        return 0
    rows = get_photo_metadata_rows(ttl_sec=_queue_rows_ttl_sec())
    serial_to_row_data: Dict[int, List[str]] = {}
    for row in rows[1:]:
        if len(row) <= COL_SERIAL:
            continue
        sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
        if sn is None:
            continue
        serial_to_row_data[int(sn)] = row

    updates: List[Dict[str, Any]] = []
    applied = 0
    for item in items:
        try:
            sn = int(item.get("serial") or 0)
        except Exception:
            continue
        row = serial_to_row_data.get(sn, [])
        if not row:
            continue
        cur_box_coords = str(row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
        cur_box_cat_ids = str(row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "").strip()
        if not cur_box_coords or cur_box_coords.lower() == "rejected":
            continue
        cur_boxes = [c for c in cur_box_coords.split("|") if str(c).strip()]
        cur_labels = [c for c in cur_box_cat_ids.split("|") if str(c).strip()]
        if cur_boxes and len(cur_labels) >= len(cur_boxes):
            # Skip if this row is no longer waiting in classify queue.
            continue
        updates.append({
            "serial": sn,
            "box_cat_ids": _build_rejected_labels(int(item.get("num_boxes") or 0)),
        })
        applied += 1

    if not updates:
        return 0
    local_photos.update_metadata_annotations(updates, "auto-quality-filter")
    try:
        force_refresh_photo_rows_cache()
    except Exception:
        pass
    return int(applied)


def _schedule_auto_reject_low_quality(items: List[Dict[str, Any]]) -> None:
    unique_items: List[Dict[str, Any]] = []
    for item in items:
        try:
            sn = int(item.get("serial") or 0)
        except Exception:
            continue
        if sn <= 0:
            continue
        if sn in _auto_reject_quality_inflight:
            continue
        _auto_reject_quality_inflight.add(sn)
        unique_items.append(item)
    if not unique_items:
        return

    async def _runner() -> None:
        try:
            applied = await asyncio.to_thread(_auto_reject_low_quality_sync, unique_items)
            if applied:
                log_action(
                    "labeler_auto_reject_quality",
                    f"applied={applied}",
                    f"pix>={_CLASSIFY_MIN_PIXELS},dim>={_CLASSIFY_MIN_DIM},blur>={_CLASSIFY_MIN_BLUR}",
                )
        except Exception as e:
            log_action("labeler_auto_reject_quality_error", "error", f"{type(e).__name__}: {e!r}")
        finally:
            for item in unique_items:
                try:
                    sn = int(item.get("serial") or 0)
                except Exception:
                    continue
                _auto_reject_quality_inflight.discard(sn)

    asyncio.create_task(_runner())


def _schedule_classify_quality_scan(items: List[Dict[str, Any]]) -> None:
    pending: List[Dict[str, Any]] = []
    for item in items:
        try:
            sn = int(item.get("serial") or 0)
        except Exception:
            continue
        if sn <= 0:
            continue
        if _cache_get_classify_quality(sn) is not None:
            continue
        if _cache_get_classify_quality_soft_fail(sn) is not None:
            continue
        if sn in _classify_quality_scan_inflight:
            continue
        _classify_quality_scan_inflight.add(sn)
        pending.append(item)
    if not pending:
        return

    async def _runner() -> None:
        sem = asyncio.Semaphore(_CLASSIFY_PREFILTER_BG_CONCURRENCY)

        async def _scan_one(item: Dict[str, Any]) -> Tuple[Dict[str, Any], Tuple[bool, Dict[str, Any]]]:
            async with sem:
                try:
                    return item, await _evaluate_classify_quality(
                        int(item.get("serial") or 0),
                        str(item.get("url") or ""),
                        source="bg_scan",
                    )
                except Exception:
                    return item, (False, {"reasons": ["scan_error"], "hard_fail": False})

        auto_reject_items: List[Dict[str, Any]] = []
        try:
            results = await asyncio.gather(*[_scan_one(item) for item in pending], return_exceptions=False)
            for item, (passes, meta) in results:
                if passes:
                    continue
                hard_fail = bool((meta or {}).get("hard_fail"))
                if not hard_fail:
                    continue
                auto_reject_items.append({
                    "serial": int(item.get("serial") or 0),
                    "num_boxes": int(item.get("num_boxes") or 0),
                    "reason": ",".join((meta or {}).get("reasons") or []),
                    "pixels": int((meta or {}).get("pixels") or 0),
                    "blur": float((meta or {}).get("blur") or 0.0),
                })
            if auto_reject_items:
                _schedule_auto_reject_low_quality(auto_reject_items)
        finally:
            for item in pending:
                try:
                    sn = int(item.get("serial") or 0)
                except Exception:
                    continue
                _classify_quality_scan_inflight.discard(sn)

    asyncio.create_task(_runner())


#---------- Queue Endpoints ----------

def _local_missing_payload(excluded: int, sample: List[int]) -> Dict[str, Any]:
    return {
        "local_missing_excluded": int(max(0, excluded)),
        "local_missing_sample": [int(sn) for sn in list(sample or [])[:_LOCAL_MISSING_SAMPLE_MAX]],
    }


def _log_local_filter_throttled(mode: str, excluded: int, sample: List[int]) -> None:
    if int(excluded) <= 0:
        return
    key = str(mode or "unknown").strip().lower() or "unknown"
    now = time.monotonic()
    next_allowed = float(_queue_local_filter_next_log_mono.get(key, 0.0))
    if now < next_allowed:
        return
    _queue_local_filter_next_log_mono[key] = now + float(_QUEUE_LOCAL_FILTER_LOG_COOLDOWN_SEC)
    sample_txt = ",".join(str(int(sn)) for sn in list(sample or [])[:_LOCAL_MISSING_SAMPLE_MAX])
    log_action(
        "labeler_queue_local_filter",
        f"mode={key}; excluded={int(excluded)}",
        f"sample={sample_txt}" if sample_txt else "sample=",
    )


def _filter_queue_to_local(
    mode: str,
    items: List[Dict[str, Any]],
    *,
    local_serials_snapshot: Set[int],
) -> Tuple[List[Dict[str, Any]], int, List[int]]:
    """Filter queue entries down to serials with locally available bytes."""
    out: List[Dict[str, Any]] = []
    excluded = 0
    sample: List[int] = []
    for item in items or []:
        try:
            sn = int(item.get("serial") or 0)
        except Exception:
            sn = 0
        if sn > 0 and sn in local_serials_snapshot:
            out.append(item)
            continue
        excluded += 1
        if sn > 0 and len(sample) < _LOCAL_MISSING_SAMPLE_MAX:
            sample.append(int(sn))
    _log_local_filter_throttled(mode, excluded, sample)
    return out, excluded, sample


def _collect_local_missing_summary(
    rows: List[List[str]],
    *,
    local_serials_snapshot: Set[int],
    sample_cap: int = _LOCAL_MISSING_SAMPLE_MAX,
) -> Dict[str, Any]:
    """Compare metadata serials against local index and return missing summary."""
    metadata_serials: Set[int] = set()
    missing: List[int] = []
    for row in rows[1:]:
        if len(row) <= COL_SERIAL:
            continue
        sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
        if sn is None:
            continue
        serial = int(sn)
        if serial in metadata_serials:
            continue
        metadata_serials.add(serial)
        if serial not in local_serials_snapshot and len(missing) < int(max(1, sample_cap)):
            missing.append(serial)
    total_missing = max(0, len(metadata_serials) - len(local_serials_snapshot.intersection(metadata_serials)))
    missing.sort()
    return {
        "total_missing": int(total_missing),
        "sample": [int(sn) for sn in missing[: int(max(1, sample_cap))]],
        "photo_root": str(local_photos.photo_root()),
    }


def _queue_rows_ttl_sec() -> int:
    """Queue reads can use a longer TTL in local-only mode to avoid network churn."""
    if local_photos.is_local_only():
        return int(_QUEUE_ROWS_TTL_LOCAL_ONLY_SEC)
    return int(_QUEUE_ROWS_TTL_SEC)


async def _get_photo_metadata_rows_async(*, force: bool = False, ttl_sec: Optional[int] = None) -> List[List[str]]:
    """Load photo metadata rows without blocking the event loop."""
    ttl = int(ttl_sec) if ttl_sec is not None else int(_queue_rows_ttl_sec())
    ttl = max(1, ttl)
    if force:
        return await asyncio.to_thread(force_refresh_photo_rows_cache)
    return await asyncio.to_thread(get_photo_metadata_rows, ttl)


async def get_queue_detect(request: web.Request) -> web.Response:
    """Return list of serials needing detector labels (empty BoxCoordinates)."""
    try:
        await _log_local_mode_once()
        _kickoff_boot_cache_warm_once()
        _kick_detector_warm_task()
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        rows = await _get_photo_metadata_rows_async(force=force)
        local_serials_snapshot = await _local_serials_async(force_refresh=force)
        user_id, _ = _actor_from_request(request)
        claims = await _claims_snapshot()
        def _parse_queue_detect_candidates(
            in_rows: List[List[str]], in_claims: Dict[Tuple[str, int], Dict[str, Any]], in_user_id: str
        ) -> List[Dict[str, Any]]:
            out_queue: List[Dict[str, Any]] = []
            for row in in_rows[1:]:  #Skip header
                if len(row) <= COL_SERIAL:
                    continue
                sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if sn is None:
                    continue
                claim = in_claims.get(("detect", int(sn)))
                if claim and str(claim.get("user_id") or "") != in_user_id:
                    continue
                box_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
                if not box_coords.strip():
                    url = row[COL_URL] if len(row) > COL_URL else ""
                    out_queue.append({"serial": sn, "url": url})
            out_queue.sort(key=lambda item: int(item.get("serial") or 0))
            return out_queue

        queue = _parse_queue_detect_candidates(rows, claims, user_id)
        queue, local_excluded, local_sample = _filter_queue_to_local(
            "detect",
            queue,
            local_serials_snapshot=local_serials_snapshot,
        )
        total = len(queue)
        queue_page = queue[:500]
        raw_context_cache = await _get_photo_item_context_cache_async(
            force=force,
            serials=[int(item.get("serial") or 0) for item in queue_page if int(item.get("serial") or 0) > 0],
        )
        queue_page = _apply_item_context_to_items(queue_page, raw_context_cache)
        #Trigger background cache fill for first images in queue (throttled)
        _maybe_schedule_queue_cache_warm("detect", queue)
        payload = {"queue": queue_page, "total": total}
        payload.update(_local_missing_payload(local_excluded, local_sample))
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_queue_detect_error", "error", str(e))
        return _with_cors(web.Response(status=500, text="Internal server error"), request)


async def get_queue_classify(request: web.Request) -> web.Response:
    """Return serials with boxes but incomplete cat IDs."""
    try:
        global _queue_classify_slow_last_log_mono
        await _log_local_mode_once()
        _kickoff_boot_cache_warm_once()
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        rows = await _get_photo_metadata_rows_async(force=force)
        local_serials_snapshot = await _local_serials_async(force_refresh=force)
        t0 = time.perf_counter()
        user_id, _ = _actor_from_request(request)
        claims = await _claims_snapshot()

        def _parse_queue_classify_candidates(
            in_rows: List[List[str]], in_claims: Dict[Tuple[str, int], Dict[str, Any]], in_user_id: str
        ) -> List[Dict[str, Any]]:
            out_candidates: List[Dict[str, Any]] = []
            for row in in_rows[1:]:
                if len(row) <= COL_SERIAL:
                    continue
                sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if sn is None:
                    continue
                claim = in_claims.get(("classify", int(sn)))
                if claim and str(claim.get("user_id") or "") != in_user_id:
                    continue
                box_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
                box_cat_ids = row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else ""
                
                if not box_coords.strip() or box_coords.strip().lower() == "rejected":
                    continue
                
                # Queue parsing must stay cheap; full box validation happens in identify/refine paths.
                num_boxes = len([b for b in str(box_coords).split("|") if str(b).strip()])
                if num_boxes <= 0:
                    continue
                labels = box_cat_ids.split("|") if box_cat_ids else []
                num_labeled = 0
                for idx in range(min(num_boxes, len(labels))):
                    if str(labels[idx] or "").strip():
                        num_labeled += 1
                
                if num_labeled < num_boxes:
                    url = row[COL_URL] if len(row) > COL_URL else ""
                    out_candidates.append({
                        "serial": sn,
                        "url": url,
                        "boxes": box_coords,
                        "labels": box_cat_ids,
                        "num_boxes": num_boxes,
                        "num_labeled": num_labeled,
                    })
            return out_candidates

        candidates = _parse_queue_classify_candidates(rows, claims, user_id)

        local_only_mode = local_photos.is_local_only()
        queue: List[Dict[str, Any]] = []
        skipped_low_quality = 0
        deferred_for_queue: List[Dict[str, Any]] = []
        if local_only_mode:
            # Local-only mode skips the quality prefilter so queue responses stay fast.
            queue = list(candidates)
        else:
            auto_reject_items: List[Dict[str, Any]] = []
            pending_items: List[Dict[str, Any]] = []
            for item in candidates:
                cached_eval = _evaluate_cached_classify_quality(int(item.get("serial") or 0))
                if cached_eval is None:
                    pending_items.append(item)
                    continue
                passes, meta = cached_eval
                if bool(passes):
                    queue.append(item)
                    continue
                skipped_low_quality += 1
                if bool((meta or {}).get("hard_fail")):
                    auto_reject_items.append({
                        "serial": int(item.get("serial") or 0),
                        "num_boxes": int(item.get("num_boxes") or 0),
                        "url": str(item.get("url") or ""),
                        "reason": str((meta or {}).get("reason") or "quality_filter"),
                    })

            # Evaluate only a small subset synchronously to keep queue endpoint fast.
            sync_limit = int(_CLASSIFY_PREFILTER_MAX_SYNC_ITEMS)
            sync_n = min(len(pending_items), int(max(0, sync_limit)))
            sync_items = pending_items[:sync_n]
            deferred_items = pending_items[sync_n:]

            if sync_items:
                quality_concurrency = max(1, min(6, int(os.getenv("LABELER_CLASSIFY_PREFILTER_CONCURRENCY", "3") or "3")))
                sem = asyncio.Semaphore(quality_concurrency)

                async def _check_one(item: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
                    async with sem:
                        try:
                            return await asyncio.wait_for(
                                _evaluate_classify_quality(
                                    int(item.get("serial") or 0),
                                    str(item.get("url") or ""),
                                    source="queue_sync",
                                ),
                                timeout=_CLASSIFY_PREFILTER_SYNC_ITEM_TIMEOUT_SEC,
                            )
                        except Exception:
                            return False, {"reasons": ["sync_timeout"], "hard_fail": False}

                checks = await asyncio.gather(*[_check_one(item) for item in sync_items], return_exceptions=False)
                for item, (passes, meta) in zip(sync_items, checks):
                    if bool(passes):
                        queue.append(item)
                        continue
                    hard_fail = bool((meta or {}).get("hard_fail"))
                    if hard_fail:
                        skipped_low_quality += 1
                        auto_reject_items.append({
                            "serial": int(item.get("serial") or 0),
                            "num_boxes": int(item.get("num_boxes") or 0),
                            "reason": ",".join((meta or {}).get("reasons") or []),
                            "pixels": int((meta or {}).get("pixels") or 0),
                            "blur": float((meta or {}).get("blur") or 0.0),
                        })
                    else:
                        deferred_items.append(item)

            # Do not hide pending quality items from the queue.
            # They remain visible immediately while background scan runs.
            if deferred_items:
                deferred_for_queue = deferred_items
                for item in deferred_for_queue:
                    row = dict(item)
                    row["quality_pending"] = True
                    queue.append(row)
                _schedule_classify_quality_scan_from_queue(deferred_for_queue)
            if auto_reject_items:
                _schedule_auto_reject_low_quality(auto_reject_items)

        t1 = time.perf_counter()
        elapsed_ms = int((t1 - t0) * 1000)
        if elapsed_ms >= int(_QUEUE_CLASSIFY_SLOW_LOG_THRESHOLD_MS):
            now_mono = time.monotonic()
            cooldown_ok = (now_mono - float(_queue_classify_slow_last_log_mono)) >= float(_QUEUE_CLASSIFY_SLOW_LOG_COOLDOWN_SEC)
            severe = elapsed_ms >= int(_QUEUE_CLASSIFY_SLOW_LOG_THRESHOLD_MS * 2)
            if cooldown_ok or severe:
                _queue_classify_slow_last_log_mono = now_mono
                log_action(
                    "labeler_queue_classify_slow",
                    "get_queue_classify",
                    f"total_ms={elapsed_ms}; candidates={len(candidates)}; queue={len(queue)}; local_only={1 if local_photos.is_local_only() else 0}",
                )

        queue, local_excluded, local_sample = _filter_queue_to_local(
            "classify",
            queue,
            local_serials_snapshot=local_serials_snapshot,
        )
        queue.sort(key=lambda item: int(item.get("serial") or 0))
        total = len(queue)
        queue_page = queue[:500]
        raw_context_cache = await _get_photo_item_context_cache_async(
            force=force,
            serials=[int(item.get("serial") or 0) for item in queue_page if int(item.get("serial") or 0) > 0],
        )
        queue_page = _apply_item_context_to_items(queue_page, raw_context_cache)
        #Trigger background cache fill for first images in queue (throttled)
        _maybe_schedule_queue_cache_warm("classify", queue)
        payload = {
            "queue": queue_page,
            "total": total,
            "filtered_low_quality": int(skipped_low_quality),
            "pending_quality_scan": int(len(deferred_for_queue)),
            "classify_min_pixels": int(_CLASSIFY_MIN_PIXELS),
            "classify_min_dim": int(_CLASSIFY_MIN_DIM),
            "classify_min_blur": float(_CLASSIFY_MIN_BLUR),
        }
        payload.update(_local_missing_payload(local_excluded, local_sample))
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_queue_classify_error", "error", str(e))
        return _internal_error_response(request)


async def get_queue_manual(request: web.Request) -> web.Response:
    """Return serials with one or more crops marked NeedsReview."""
    try:
        await _log_local_mode_once()
        _kickoff_boot_cache_warm_once()
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        rows = await _get_photo_metadata_rows_async(force=force)
        local_serials_snapshot = await _local_serials_async(force_refresh=force)
        user_id, _ = _actor_from_request(request)
        claims = await _claims_snapshot()
        def _parse_queue_manual_candidates(
            in_rows: List[List[str]], in_claims: Dict[Tuple[str, int], Dict[str, Any]], in_user_id: str
        ) -> List[Dict[str, Any]]:
            out_queue: List[Dict[str, Any]] = []
            for row in in_rows[1:]:
                if len(row) <= COL_SERIAL:
                    continue
                sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if sn is None:
                    continue
                claim = in_claims.get(("manual", int(sn)))
                if claim and str(claim.get("user_id") or "") != in_user_id:
                    continue
                box_coords = str(row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
                box_cat_ids = str(row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "").strip()
                if not box_coords or box_coords.lower() == "rejected" or not box_cat_ids:
                    continue
                coords = [c.strip() for c in box_coords.split("|")]
                labels = [l.strip() for l in box_cat_ids.split("|")]
                if not coords or not labels:
                    continue
                review_indices = [
                    idx for idx in range(min(len(coords), len(labels)))
                    if _is_needs_review_label(labels[idx])
                ]
                if not review_indices:
                    continue
                url = row[COL_URL] if len(row) > COL_URL else ""
                out_queue.append({
                    "serial": sn,
                    "url": url,
                    "boxes": box_coords,
                    "labels": box_cat_ids,
                    "num_boxes": len(coords),
                    "review_indices": review_indices,
                    "num_review": len(review_indices),
                })
            out_queue.sort(key=lambda item: int(item.get("serial") or 0))
            return out_queue

        queue = _parse_queue_manual_candidates(rows, claims, user_id)
        queue, local_excluded, local_sample = _filter_queue_to_local(
            "manual",
            queue,
            local_serials_snapshot=local_serials_snapshot,
        )
        total = len(queue)
        queue_page = queue[:500]
        raw_context_cache = await _get_photo_item_context_cache_async(
            force=force,
            serials=[int(item.get("serial") or 0) for item in queue_page if int(item.get("serial") or 0) > 0],
        )
        queue_page = _apply_item_context_to_items(queue_page, raw_context_cache)
        _maybe_schedule_queue_cache_warm("manual", queue)
        payload = {"queue": queue_page, "total": total}
        payload.update(_local_missing_payload(local_excluded, local_sample))
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_queue_manual_error", "error", str(e))
        return _internal_error_response(request)


async def get_local_missing(request: web.Request) -> web.Response:
    """Return summary of metadata serials missing from local photo root."""
    try:
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        rows = await _get_photo_metadata_rows_async(force=force, ttl_sec=60)
        local_serials_snapshot = await _local_serials_async(force_refresh=force)
        payload = _collect_local_missing_summary(
            rows,
            local_serials_snapshot=local_serials_snapshot,
            sample_cap=_LOCAL_MISSING_SAMPLE_MAX,
        )
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_local_missing_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


async def get_image(request: web.Request) -> web.Response:
    """Get image data and annotations for a specific serial."""
    try:
        sn_str = request.match_info.get("sn", "")
        sn = _parse_serial(sn_str)
        if sn is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)
        
        rows = await _get_photo_metadata_rows_async(ttl_sec=60)
        def _get_image_data_from_rows(in_rows: List[List[str]], target_sn: int) -> Optional[Dict[str, Any]]:
            for row in in_rows[1:]:
                if len(row) <= COL_SERIAL:
                    continue
                row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if row_sn == target_sn:
                    return {
                        "serial": target_sn,
                        "url": row[COL_URL] if len(row) > COL_URL else "",
                        "cat_id": row[COL_CAT_ID] if len(row) > COL_CAT_ID else "",
                        "box_coords": row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "",
                        "box_cat_ids": row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "",
                    }
            return None

        data = await asyncio.to_thread(_get_image_data_from_rows, rows, sn)
        if data:
            return _with_cors(web.json_response(data), request)
        
        return _with_cors(web.Response(status=404, text="Serial not found"), request)
    except Exception as e:
        log_action("labeler_get_image_error", "error", str(e))
        return _internal_error_response(request)


async def get_item_context(request: web.Request) -> web.Response:
    """Resolve author/channel context for one serial."""
    try:
        sn_str = request.match_info.get("sn", "")
        sn = _parse_serial(sn_str)
        if sn is None or int(sn) <= 0:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        payload = await _get_resolved_item_context_payload(
            int(sn),
            force_raw=force,
            allow_fetch=True,
        )
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_get_item_context_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


async def get_cached_image(request: web.Request) -> web.Response:
    """Get image bytes for a serial from local storage or local cache."""
    try:
        await _log_local_mode_once()
        sn_str = request.match_info.get("sn", "")
        sn = _parse_serial(sn_str)
        if sn is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)

        # Local source of truth first. Resolving a path can rescan the photo
        # library, so it has to stay off the event loop -- this endpoint is hit
        # once per queue item and the UI prefetches several at a time.
        local_path = await asyncio.to_thread(local_photos.get_local_photo_path, int(sn))
        if local_path is not None:
            try:
                local_data = await asyncio.to_thread(local_path.read_bytes)
            except Exception:
                local_data = None
            if local_data:
                resp = web.Response(
                    body=local_data,
                    content_type=local_photos.content_type_for_path(local_path),
                )
                resp.headers["Cache-Control"] = "no-store"
                resp.headers["X-Labeler-Cache"] = "local"
                resp.headers["X-Labeler-Image-Path"] = "local"
                return _with_cors(resp, request)

        data = await labeler_cache.get_cached_image_async(sn)
        if data:
            resp = web.Response(body=data, content_type="image/jpeg")
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Labeler-Cache"] = "cache"
            resp.headers["X-Labeler-Image-Path"] = "cache"
            return _with_cors(resp, request)

        log_action(
            "labeler_local_image_missing",
            f"sn={int(sn)}",
            f"root={str(local_photos.photo_root())}",
        )
        resp = web.json_response(
            {"error": "local_image_missing", "serial": int(sn)},
            status=404,
        )
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Labeler-Cache"] = "local-miss"
        resp.headers["X-Labeler-Image-Path"] = "local-missing"
        return _with_cors(resp, request)
    except Exception as e:
        log_action("labeler_cached_image_error", "error", str(e))
        return _internal_error_response(request)


async def get_ref_crop(request: web.Request) -> web.Response:
    """Return one metadata-defined crop as JPEG for classifier/manual reference cards."""
    try:
        req_t0 = time.perf_counter()
        sn = _parse_serial(str(request.match_info.get("sn", "")).strip())
        if sn is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)
        try:
            crop_num = int(str(request.match_info.get("crop", "")).strip())
        except Exception:
            crop_num = 0
        if crop_num <= 0:
            return _with_cors(web.Response(status=400, text="Invalid crop"), request)
        if _is_flagged_ref_serial(int(sn)):
            return _with_cors(web.Response(status=410, text="Reference blacklisted pending relabel"), request)

        thumb_size = max(
            48,
            min(
                512,
                int(request.query.get("size") or getattr(settings, "labeler_ref_thumb_size", 128) or 128),
            ),
        )
        cache_key = _ref_crop_cache_key(int(sn), int(crop_num), int(thumb_size))
        cached = _cache_get_bytes(
            _ref_crop_result_cache,
            cache_key,
            ttl_sec=_REF_CROP_RESULT_TTL_SEC,
        )
        if cached:
            return _with_cors(web.Response(body=cached, content_type="image/jpeg"), request)

        image_fetch_ms = 0.0
        render_ms = 0.0
        sem_wait_ms = 0.0
        image_source = "none"

        # Avoid spamming downstream storage/network and logs for known-bad refs.
        neg_key = (int(sn), int(crop_num))
        bad_until = _ref_crop_negative_cache.get(neg_key, 0.0)
        if bad_until and time.monotonic() < float(bad_until):
            return _with_cors(web.Response(status=404, text="Crop not found"), request)

        await _ensure_photo_crop_index_cache(force=False)
        entry = _photo_crop_index_cache.get((int(sn), int(crop_num)))
        if not entry:
            await _refresh_photo_crop_index_after_miss()
            entry = _photo_crop_index_cache.get((int(sn), int(crop_num)))
        if not entry:
            _log_ref_crop_miss(int(sn), int(crop_num), "photo_entry_missing")
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 600.0
            return _with_cors(web.Response(status=404, text="Crop not found"), request)

        box = _parse_yolo_box_str(str(entry.get("box") or "").strip())
        if box is None:
            _log_ref_crop_miss(int(sn), int(crop_num), "box_missing")
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 600.0
            return _with_cors(web.Response(status=404, text="Crop coordinates missing"), request)

        t_image = time.perf_counter()
        image_bytes = await labeler_cache.get_cached_image_async(int(sn))
        if image_bytes:
            image_source = "cache"
        if not image_bytes:
            image_bytes = await _fetch_image_bytes_for_labeler(
                int(sn),
                str(entry.get("url") or ""),
                bypass_backoff=True,
            )
            if image_bytes:
                image_source = "local_fetch"
        image_fetch_ms = (time.perf_counter() - t_image) * 1000.0
        if not image_bytes:
            _log_ref_crop_miss(int(sn), int(crop_num), "image_unavailable")
            # Treat source-image fetch failures as transient; avoid long false negatives.
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 20.0
            return _with_cors(web.Response(status=502, text="Source image unavailable"), request)

        acquired = False
        try:
            t_sem = time.perf_counter()
            await _ref_crop_render_sem.acquire()
            acquired = True
            sem_wait_ms = (time.perf_counter() - t_sem) * 1000.0
            t_render = time.perf_counter()
            payload, crop_err, crop_detail = await asyncio.to_thread(
                _render_ref_crop_jpeg,
                image_bytes,
                box,
                int(thumb_size),
                float(settings.cv_pad_pct),
            )
            render_ms = (time.perf_counter() - t_render) * 1000.0
        finally:
            if acquired:
                _ref_crop_render_sem.release()
        if not payload:
            if str(crop_err or "") == "invalid_bounds":
                _log_ref_crop_miss(
                    int(sn),
                    int(crop_num),
                    "invalid_bounds",
                    str(crop_detail or ""),
                )
                _ref_crop_negative_cache[neg_key] = time.monotonic() + 600.0
                return _with_cors(web.Response(status=422, text="Invalid crop bounds"), request)
            return _with_cors(web.Response(status=500, text="Failed to render reference crop"), request)

        _cache_set_bytes(
            _ref_crop_result_cache,
            cache_key,
            payload,
            max_items=_REF_CROP_RESULT_CACHE_MAX,
            ttl_sec=_REF_CROP_RESULT_TTL_SEC,
        )
        _remember_ref_crop_cache_key(int(sn), cache_key)
        return _with_cors(web.Response(body=payload, content_type="image/jpeg"), request)
    except Exception as e:
        log_action("labeler_ref_crop_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


async def post_ui_diag(request: web.Request) -> web.Response:
    """Temporary UI diagnostics endpoint for warm/progress investigations."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        event = str(data.get("event") or "ui_diag").strip().lower()[:64] or "ui_diag"
        always_log_events = {
            "auto_skip",
            "auto_skip_halt",
            "image_error",
            "transition_slow",
            "claim_retry",
            "claim_acquire_slow",
            "claim_acquire_error",
            "detect_item_ready_done",
            "detect_item_ready_timeout",
            "detect_next_prime",
            "detect_ready_refine_error",
            "classify_item_ready_done",
            "classify_item_ready_timeout",
            #TEMPORARY: sidebar render-cost measurement. Allowlisted so the
            #summaries land in the log without turning on verbose UI diag for
            #everything else. Drop this line when the instrumentation comes out.
            "render_perf",
        }
        if _UI_DIAG_VERBOSE or event in always_log_events:
            mode = str(data.get("mode") or "").strip().lower()[:24]
            serial = _parse_serial(str(data.get("serial") or ""))
            detail = str(data.get("detail") or data.get("details") or "").strip()
            if event == "transition_slow":
                detail_obj: Dict[str, Any]
                try:
                    parsed = json.loads(detail) if detail else {}
                    detail_obj = parsed if isinstance(parsed, dict) else {"detail": detail}
                except Exception:
                    detail_obj = {"detail": detail}
                detail_obj["server"] = _labeler_runtime_snapshot()
                detail = json.dumps(detail_obj, separators=(",", ":"))
            if len(detail) > 8000:
                detail = detail[:8000]
            user_id, actor = _actor_from_request(request)
            log_action(
                f"labeler_ui_{event}",
                f"mode={mode}; serial={serial}; uid={user_id or ''}; actor={actor}",
                detail,
            )
    except Exception as e:
        log_action("labeler_ui_diag_error", "error", f"{type(e).__name__}: {e!r}")
    return _with_cors(web.json_response({"ok": True}), request)


async def post_claim(request: web.Request) -> web.Response:
    """Acquire/heartbeat/release a claim for a queue item in detect/classify/manual mode."""
    _ensure_loop_lag_monitor()
    try:
        t0 = time.perf_counter()
        loop_lag_at_entry = _loop_lag_ms
        try:
            data = await request.json()
        except Exception:
            data = {}
        t_json = time.perf_counter()
        mode = str(data.get("mode") or "detect").strip().lower()
        if mode not in {"detect", "classify", "manual"}:
            return _with_cors(web.Response(status=400, text="Invalid mode"), request)

        serial = _parse_serial(str(data.get("serial") or ""))
        if serial is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)

        action = str(data.get("action") or "acquire").strip().lower()
        user_id, username = _actor_from_request(request)
        if not user_id:
            log_action(
                "labeler_claim_auth_missing",
                f"mode={mode}; action={action}; serial={int(serial)}",
                "Missing session user",
            )
            return _with_cors(web.Response(status=401, text="Missing session user"), request)

        if action == "release":
            released = await _release_claim(mode, int(serial), user_id)
            dt_ms = int(round((time.perf_counter() - t0) * 1000.0))
            if dt_ms >= 800:
                log_action(
                    "labeler_claim_release_slow",
                    f"mode={mode}; serial={int(serial)}; uid={user_id}",
                    f"ms={dt_ms}; released={1 if released else 0}",
                )
            return _with_cors(web.json_response({
                "ok": True,
                "released": bool(released),
                "mode": mode,
                "serial": int(serial),
            }), request)

        t_pre_acquire = time.perf_counter()
        granted, owner = await _acquire_claim(mode, int(serial), user_id, username)
        t_post_acquire = time.perf_counter()
        dt_ms = int(round((t_post_acquire - t0) * 1000.0))
        json_ms = int(round((t_json - t0) * 1000.0))
        acquire_ms = int(round((t_post_acquire - t_pre_acquire) * 1000.0))
        lag_ms = int(loop_lag_at_entry)
        # Keep claim diagnostics lightweight: always record slow/laggy claims,
        # otherwise sample a small share for visibility.
        is_slow = dt_ms >= int(_CLAIM_DIAG_SLOW_MS)
        is_laggy = lag_ms >= int(_LOOP_LAG_SHED_BG_WORK_MS)
        should_log_diag = (
            (action != "heartbeat" and (is_slow or is_laggy or random.random() < float(_CLAIM_DIAG_SAMPLE)))
            or (action == "heartbeat" and (is_slow or is_laggy))
        )
        if should_log_diag:
            include_census = (action != "heartbeat") and (is_slow or (lag_ms >= 900))
            task_census = _get_task_census() if include_census else "tasks=skip"
            dl_stats = _labeler_download_stats()
            log_action(
                "labeler_claim_diag",
                f"mode={mode}; serial={int(serial)}; action={action}",
                (
                    f"total_ms={dt_ms}; json_ms={json_ms}; acquire_ms={acquire_ms}; "
                    f"loop_lag_ms={lag_ms}; granted={1 if granted else 0}; "
                    f"dl=[{dl_stats}]; {task_census}"
                ),
            )
        return _with_cors(web.json_response({
            "ok": True,
            "granted": bool(granted),
            "mode": mode,
            "serial": int(serial),
            "claimed_by": owner.get("username") if owner else None,
            "claim_ttl_sec": _LABELER_CLAIM_TTL_SEC,
        }), request)
    except Exception as e:
        try:
            mode_s = str(locals().get("mode") or "")
            action_s = str(locals().get("action") or "")
            serial_s = str(locals().get("serial") or "")
            trig = f"mode={mode_s}; action={action_s}; serial={serial_s}"
        except Exception:
            trig = "error"
        log_action("labeler_claim_error", trig, f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


#---------- CV Endpoints ----------

async def post_detect(request: web.Request) -> web.Response:
    """Run YOLO+SAM detection on an image. Accepts serial (preferred) or URL."""
    try:
        data = await request.json()
        serial = data.get("serial")
        url = data.get("url")
        fast = bool(data.get("fast"))
        prefetch = bool(data.get("prefetch"))
        serial_i = _parse_serial(str(serial or ""))
        req_id = _hash_cache_key(
            "detect_req",
            time.time_ns(),
            serial_i or "",
            str(url or "").strip(),
            int(bool(prefetch)),
            int(bool(fast)),
        )[:10]
        t_req = time.perf_counter()
        image_ms = 0.0
        detect_ms = 0.0
        sem_wait_ms = 0.0
        image_source = "none"
        inline_sam_passes_requested = int(_DETECT_INLINE_SAM_PASSES)

        cache_key = _hash_cache_key(
            "detect",
            _DETECT_PIPELINE_VERSION,
            serial_i or "",
            str(url or "").strip(),
            int(bool(fast)),
        )
        cached_payload = _cache_get(_detect_result_cache, cache_key)
        if cached_payload is not None:
            cached_boxes = [
                b for b in str(cached_payload.get("boxes_yolo") or "").split("|") if str(b).strip()
            ]
            cached_raw_boxes = [
                b for b in str(cached_payload.get("raw_boxes_yolo") or cached_payload.get("boxes_yolo") or "").split("|")
                if str(b).strip()
            ]
            cached_diag = cached_payload.get("detect_diag") if isinstance(cached_payload.get("detect_diag"), dict) else {}
            cached_payload["detect_diag"] = {
                **dict(cached_diag or {}),
                "served_rid": req_id,
                "cache_hit": True,
            }
            log_action(
                "labeler_detect_cache_hit",
                f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; fast={fast}",
                (
                    f"boxes={len(cached_boxes)}; raw_boxes={len(cached_raw_boxes)}; "
                    f"cached_compute_rid={str(cached_diag.get('compute_rid') or '')}"
                ),
            )
            return _with_cors(web.json_response(cached_payload), request)

        image_bytes = None
        t_image = time.perf_counter()
        
        #Try cache first if serial provided
        if serial_i is not None:
            image_bytes = await labeler_cache.get_cached_image_async(int(serial_i))
            if image_bytes:
                image_source = "cache"

        #If serial provided but not cached and no URL, look up URL by serial
        if serial_i is not None and not image_bytes and not url:
            rows = await _get_photo_metadata_rows_async(ttl_sec=_queue_rows_ttl_sec())
            for row in rows[1:]:
                if len(row) <= COL_SERIAL:
                    continue
                row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if row_sn == int(serial_i):
                    url = row[COL_URL] if len(row) > COL_URL else ""
                    break

        if not image_bytes:
            image_bytes = await _fetch_image_bytes_for_labeler(
                serial_i,
                url,
                bypass_backoff=True,
            )
            if image_bytes:
                image_source = "fetch"
        image_ms = (time.perf_counter() - t_image) * 1000.0
        
        if not image_bytes:
            return _with_cors(web.Response(status=400, text="No image available"), request)

        log_action(
            "labeler_detect_start",
            f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; fast={fast}",
            f"img_src={image_source}; img_ms={int(round(image_ms))}",
        )

        acquired = False
        raw_boxes_abs: List[Tuple[float, float, float, float]] = []
        refined_boxes_abs: List[Tuple[float, float, float, float]] = []
        refined_polygons_abs: List[List[Tuple[float, float]]] = []
        refined_mask_tiles_abs: List[Dict[str, Any]] = []
        boxed_jpeg: bytes = b""
        sam_refined: bool = False
        sam_summary: Dict[str, Any] = {}
        working_image_size: Tuple[int, int] = (0, 0)
        try:
            t_sem = time.perf_counter()
            await _detect_sem.acquire()
            acquired = True
            sem_wait_ms = (time.perf_counter() - t_sem) * 1000.0

            # Run YOLO once, then finish the same request with SAM before returning boxes.
            detect_timeout = _DETECT_PREFETCH_TIMEOUT_SEC if prefetch else _DETECT_TIMEOUT_SEC
            t_detect = time.perf_counter()
            detect_result = await asyncio.wait_for(
                asyncio.to_thread(V.detect, image_bytes, include_boxed_image=False),
                timeout=detect_timeout,
            )
            detect_ms = (time.perf_counter() - t_detect) * 1000.0
            boxed_jpeg = detect_result.boxed_jpeg or b""
            working_image_size = tuple(getattr(detect_result, "image_size", ()) or ())
            raw_boxes_abs = []
            for r in getattr(detect_result, "results", []) or []:
                box = (r or {}).get("box")
                if not box or len(box) != 4:
                    continue
                try:
                    x1, y1, x2, y2 = [float(v) for v in box]
                except Exception:
                    continue
                raw_boxes_abs.append((x1, y1, x2, y2))

            # Always finish detect items with SAM before surfacing boxes.
            refined_boxes_abs = list(raw_boxes_abs)
            if len(working_image_size) != 2 or int(working_image_size[0]) <= 0 or int(working_image_size[1]) <= 0:
                working_image_size = await asyncio.to_thread(_vision_working_image_size, image_bytes)
            if (not fast) and raw_boxes_abs:
                iw, ih = [int(v) for v in working_image_size]
                yolo_boxes: List[Tuple[float, float, float, float]] = []
                for (x1, y1, x2, y2) in raw_boxes_abs:
                    cx = (x1 + x2) / 2 / iw
                    cy = (y1 + y2) / 2 / ih
                    w = (x2 - x1) / iw
                    h = (y2 - y1) / ih
                    yolo_boxes.append((cx, cy, w, h))
                try:
                    sam_timeout = max(1.0, float(_DETECT_INLINE_SAM_TIMEOUT_SEC))
                    refine_result = await asyncio.wait_for(
                        asyncio.to_thread(
                            V.refine_boxes_with_diagnostics,
                            image_bytes,
                            yolo_boxes,
                            passes=int(inline_sam_passes_requested),
                        ),
                        timeout=sam_timeout,
                    )
                    refined_boxes_abs = list(refine_result.boxes or raw_boxes_abs)
                    refined_polygons_abs = list(refine_result.polygons or [])
                    refined_mask_tiles_abs = list(refine_result.mask_tiles or [])
                    sam_summary = dict(refine_result.summary or {})
                    sam_refined = True
                except Exception as e:
                    error_kind, error_text = _compact_exception_diag(e)
                    log_action(
                        "labeler_detect_inline_sam_error",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; fast={fast}",
                        (
                            f"reason={error_kind}; msg={error_text}; boxes={len(raw_boxes_abs)}; "
                            f"passes={int(inline_sam_passes_requested)}; timeout_sec={sam_timeout:.1f}"
                        ),
                    )
                    if isinstance(e, asyncio.TimeoutError):
                        raise web.HTTPGatewayTimeout(text="Detect SAM refine timed out")
                    raise web.HTTPServiceUnavailable(text="Detect SAM refine failed")
            elif not fast:
                # No boxes to refine still counts as full detect path.
                sam_refined = True
                sam_summary = {
                    "passes": int(inline_sam_passes_requested),
                    "boxes": 0,
                    "accepted_boxes": 0,
                    "fallback_boxes": 0,
                    "clipped_boxes": 0,
                    "guard_reject_boxes": 0,
                    "candidate_masks": 0,
                    "accepted_masks": 0,
                    "selected": {"tight": 0, "iou": 0, "fallback": 0},
                    "max_outside_guard_ratio": 0.0,
                    "max_detector_coverage": 0.0,
                    "max_area_ratio": 1.0,
                    "samples": [],
                }
        finally:
            if acquired:
                _detect_sem.release()
        
        #Encode boxed image as base64
        import base64
        boxed_b64 = base64.b64encode(boxed_jpeg).decode("ascii") if boxed_jpeg else ""
        
        #Convert boxes to YOLO normalized format (cx, cy, w, h)
        if len(working_image_size) != 2 or int(working_image_size[0]) <= 0 or int(working_image_size[1]) <= 0:
            working_image_size = await asyncio.to_thread(_vision_working_image_size, image_bytes)
        iw, ih = [int(v) for v in working_image_size]

        raw_yolo_boxes = []
        for (x1, y1, x2, y2) in raw_boxes_abs:
            cx = (x1 + x2) / 2 / iw
            cy = (y1 + y2) / 2 / ih
            w = (x2 - x1) / iw
            h = (y2 - y1) / ih
            raw_yolo_boxes.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

        yolo_boxes = []
        boxes_out = refined_boxes_abs if refined_boxes_abs else raw_boxes_abs
        for (x1, y1, x2, y2) in boxes_out:
            cx = (x1 + x2) / 2 / iw
            cy = (y1 + y2) / 2 / ih
            w = (x2 - x1) / iw
            h = (y2 - y1) / ih
            yolo_boxes.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        if len(refined_polygons_abs) < len(yolo_boxes):
            refined_polygons_abs.extend([[] for _ in range(len(yolo_boxes) - len(refined_polygons_abs))])
        sam_polygons = _normalize_abs_polygons(refined_polygons_abs[:len(yolo_boxes)], iw, ih)
        if len(refined_mask_tiles_abs) < len(yolo_boxes):
            refined_mask_tiles_abs.extend([{} for _ in range(len(yolo_boxes) - len(refined_mask_tiles_abs))])
        sam_mask_tiles = _normalize_abs_mask_tiles(refined_mask_tiles_abs[:len(yolo_boxes)], iw, ih)

        box_delta = _box_delta_summary(raw_boxes_abs, boxes_out)
        detect_diag = {
            "compute_rid": req_id,
            "served_rid": req_id,
            "cache_hit": False,
            "image_source": image_source,
            "raw_box_count": int(len(raw_boxes_abs)),
            "out_box_count": int(len(yolo_boxes)),
            "sam_refined": bool(sam_refined),
            "sam_summary": dict(sam_summary or {}),
            "box_delta": box_delta,
        }
        payload = {
            "boxed_image": boxed_b64,
            "boxes": yolo_boxes,
            "boxes_yolo": "|".join(yolo_boxes),
            "raw_boxes": raw_yolo_boxes,
            "raw_boxes_yolo": "|".join(raw_yolo_boxes),
            "sam_polygons": sam_polygons,
            "sam_mask_tiles": sam_mask_tiles,
            "sam_refined": bool(sam_refined),
            "sam_inline_passes_requested": int(inline_sam_passes_requested),
            "detect_diag": detect_diag,
        }
        _cache_set(
            _detect_result_cache,
            cache_key,
            payload,
            max_items=_DETECT_RESULT_CACHE_MAX,
        )
        total_ms = (time.perf_counter() - t_req) * 1000.0
        log_action(
            "labeler_detect_done",
            f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; fast={fast}",
            (
                f"ms total={int(round(total_ms))} img={int(round(image_ms))} sem={int(round(sem_wait_ms))} "
                f"detect={int(round(detect_ms))}; src={image_source}; raw_boxes={len(raw_boxes_abs)}; "
                f"out_boxes={len(yolo_boxes)}; sam_refined={int(bool(sam_refined))}; "
                f"sam_passes={int(inline_sam_passes_requested)}; "
                f"delta_shifted={int(box_delta.get('shifted') or 0)}; "
                f"delta_max_center_px={float(box_delta.get('max_center_shift_px') or 0.0):.2f}; "
                f"delta_max_edge_px={float(box_delta.get('max_edge_shift_px') or 0.0):.2f}; "
                f"sam=[{_compact_refine_summary(sam_summary)}]"
            ),
        )
        return _with_cors(web.json_response(payload), request)
    except web.HTTPException as e:
        return _with_cors(web.Response(status=e.status, text=str(e.text or e.reason or "Request failed")), request)
    except Exception as e:
        log_action("labeler_detect_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


async def post_refine(request: web.Request) -> web.Response:
    """Refine provided boxes using SAM."""
    try:
        data = await request.json()
        serial = data.get("serial")
        url = data.get("url")
        boxes_raw = data.get("boxes", [])
        passes = int(data.get("passes") or 1)
        prefetch = bool(data.get("prefetch"))
        serial_i = _parse_serial(str(serial or ""))
        req_id = _hash_cache_key(
            "refine_req",
            time.time_ns(),
            serial_i or "",
            str(url or "").strip(),
            int(passes),
            int(bool(prefetch)),
            "|".join(str(b).strip() for b in list(boxes_raw or [])),
        )[:10]
        t_req = time.perf_counter()

        boxes_sig = "|".join(str(b).strip() for b in boxes_raw)
        cache_key = _hash_cache_key(
            "refine",
            _REFINE_PIPELINE_VERSION,
            serial_i or "",
            str(url or "").strip(),
            int(passes),
            boxes_sig,
        )
        cached_payload = _cache_get(_refine_result_cache, cache_key)
        if cached_payload is not None:
            cached_diag = cached_payload.get("refine_diag") if isinstance(cached_payload.get("refine_diag"), dict) else {}
            cached_payload["refine_diag"] = {
                **dict(cached_diag or {}),
                "served_rid": req_id,
                "cache_hit": True,
            }
            log_action(
                "labeler_refine_cache_hit",
                f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; passes={passes}",
                (
                    f"boxes={len([b for b in str(cached_payload.get('boxes_yolo') or '').split('|') if str(b).strip()])}; "
                    f"cached_compute_rid={str(cached_diag.get('compute_rid') or '')}"
                ),
            )
            return _with_cors(web.json_response(cached_payload), request)

        boxes: List[Tuple[float, float, float, float]] = []
        for b in boxes_raw:
            try:
                parts = [float(p) for p in str(b).strip().split()]
            except Exception:
                continue
            if len(parts) == 4:
                boxes.append((parts[0], parts[1], parts[2], parts[3]))

        image_bytes = None
        if serial_i is not None:
            image_bytes = await labeler_cache.get_cached_image_async(int(serial_i))

        if serial_i is not None and not image_bytes and not url:
            rows = await _get_photo_metadata_rows_async(ttl_sec=_queue_rows_ttl_sec())
            for row in rows[1:]:
                if len(row) <= COL_SERIAL:
                    continue
                row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if row_sn == int(serial_i):
                    url = row[COL_URL] if len(row) > COL_URL else ""
                    break

        if not image_bytes:
            image_bytes = await _fetch_image_bytes_for_labeler(serial_i, url)

        if not image_bytes:
            #Fail soft: keep existing detector boxes if source image fetch is unavailable.
            yolo_boxes = _boxes_to_yolo_strings(boxes)
            if yolo_boxes:
                payload = {
                    "boxes": yolo_boxes,
                    "boxes_yolo": "|".join(yolo_boxes),
                    "sam_polygons": [[] for _ in range(len(yolo_boxes))],
                    "sam_mask_tiles": [{} for _ in range(len(yolo_boxes))],
                    "fallback": "passthrough_no_image",
                    "refine_diag": {
                        "compute_rid": req_id,
                        "served_rid": req_id,
                        "cache_hit": False,
                        "input_box_count": int(len(boxes)),
                        "out_box_count": int(len(yolo_boxes)),
                        "box_delta": {
                            "pairs": int(len(boxes)),
                            "shifted": 0,
                            "max_center_shift_px": 0.0,
                            "max_edge_shift_px": 0.0,
                            "max_area_ratio": 1.0,
                        },
                        "sam_summary": {
                            "passes": int(max(1, passes)),
                            "boxes": int(len(boxes)),
                            "accepted_boxes": 0,
                            "fallback_boxes": int(len(boxes)),
                            "clipped_boxes": 0,
                            "guard_reject_boxes": 0,
                            "candidate_masks": 0,
                            "accepted_masks": 0,
                            "selected": {"tight": 0, "iou": 0, "fallback": int(len(boxes))},
                            "max_outside_guard_ratio": 0.0,
                            "max_detector_coverage": 0.0,
                            "max_area_ratio": 1.0,
                            "samples": [{"box_index": 0, "selected": "fallback", "reason": "passthrough_no_image"}]
                            if boxes else [],
                        },
                    },
                }
                _cache_set(
                    _refine_result_cache,
                    cache_key,
                    payload,
                    max_items=_REFINE_RESULT_CACHE_MAX,
                )
                return _with_cors(web.json_response(payload), request)
            return _with_cors(web.Response(status=400, text="No image available"), request)

        acquired = False
        sam_summary: Dict[str, Any] = {}
        refined: List[Tuple[float, float, float, float]] = []
        refined_polygons_abs: List[List[Tuple[float, float]]] = []
        refined_mask_tiles_abs: List[Dict[str, Any]] = []
        try:
            await _refine_sem.acquire()
            acquired = True

            try:
                refine_result = await asyncio.wait_for(
                    asyncio.to_thread(V.refine_boxes_with_diagnostics, image_bytes, boxes, passes=passes),
                    timeout=(_REFINE_PREFETCH_TIMEOUT_SEC if prefetch else _REFINE_TIMEOUT_SEC),
                )
                refined = list(refine_result.boxes or [])
                refined_polygons_abs = list(refine_result.polygons or [])
                refined_mask_tiles_abs = list(refine_result.mask_tiles or [])
                sam_summary = dict(refine_result.summary or {})
            except Exception as e:
                error_kind, error_text = _compact_exception_diag(e)
                log_action(
                    "labeler_refine_error_stage",
                    f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; passes={passes}",
                    (
                        f"reason={error_kind}; msg={error_text}; boxes={len(boxes)}; "
                        f"timeout_sec={(_REFINE_PREFETCH_TIMEOUT_SEC if prefetch else _REFINE_TIMEOUT_SEC):.1f}"
                    ),
                )
                if isinstance(e, asyncio.TimeoutError):
                    raise web.HTTPGatewayTimeout(text="SAM refine timed out")
                raise web.HTTPServiceUnavailable(text="SAM refine failed")
        finally:
            if acquired:
                _refine_sem.release()

        iw, ih = _vision_working_image_size(image_bytes)
        yolo_boxes = []
        for (x1, y1, x2, y2) in refined:
            cx = (x1 + x2) / 2 / iw
            cy = (y1 + y2) / 2 / ih
            w = (x2 - x1) / iw
            h = (y2 - y1) / ih
            yolo_boxes.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        if len(refined_polygons_abs) < len(yolo_boxes):
            refined_polygons_abs.extend([[] for _ in range(len(yolo_boxes) - len(refined_polygons_abs))])
        sam_polygons = _normalize_abs_polygons(refined_polygons_abs[:len(yolo_boxes)], iw, ih)
        if len(refined_mask_tiles_abs) < len(yolo_boxes):
            refined_mask_tiles_abs.extend([{} for _ in range(len(yolo_boxes) - len(refined_mask_tiles_abs))])
        sam_mask_tiles = _normalize_abs_mask_tiles(refined_mask_tiles_abs[:len(yolo_boxes)], iw, ih)

        input_boxes_abs: List[Tuple[float, float, float, float]] = []
        for (cx, cy, w, h) in boxes:
            x1 = (cx - w / 2) * iw
            y1 = (cy - h / 2) * ih
            x2 = (cx + w / 2) * iw
            y2 = (cy + h / 2) * ih
            input_boxes_abs.append((x1, y1, x2, y2))
        box_delta = _box_delta_summary(input_boxes_abs, refined)
        payload = {
            "boxes": yolo_boxes,
            "boxes_yolo": "|".join(yolo_boxes),
            "sam_polygons": sam_polygons,
            "sam_mask_tiles": sam_mask_tiles,
            "refine_diag": {
                "compute_rid": req_id,
                "served_rid": req_id,
                "cache_hit": False,
                "input_box_count": int(len(boxes)),
                "out_box_count": int(len(yolo_boxes)),
                "box_delta": box_delta,
                "sam_summary": dict(sam_summary or {}),
            },
        }
        _cache_set(
            _refine_result_cache,
            cache_key,
            payload,
            max_items=_REFINE_RESULT_CACHE_MAX,
        )
        total_ms = (time.perf_counter() - t_req) * 1000.0
        log_action(
            "labeler_refine_done",
            f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; passes={passes}",
            (
                f"ms={int(round(total_ms))}; in_boxes={len(boxes)}; out_boxes={len(yolo_boxes)}; "
                f"delta_shifted={int(box_delta.get('shifted') or 0)}; "
                f"delta_max_center_px={float(box_delta.get('max_center_shift_px') or 0.0):.2f}; "
                f"delta_max_edge_px={float(box_delta.get('max_edge_shift_px') or 0.0):.2f}; "
                f"sam=[{_compact_refine_summary(sam_summary)}]"
            ),
        )
        return _with_cors(web.json_response(payload), request)
    except web.HTTPException as e:
        return _with_cors(web.Response(status=e.status, text=str(e.text or e.reason or "Request failed")), request)
    except Exception as e:
        log_action("labeler_refine_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


async def post_identify(request: web.Request) -> web.Response:
    """Run DINOv3 identification on crops from an image."""
    try:
        global _identify_prefetch_timeout_streak, _identify_prefetch_backoff_until_mono
        data = await request.json()
        serial = data.get("serial")
        url = data.get("url")
        prefetch = bool(data.get("prefetch"))
        rerank = bool(data.get("rerank", True))
        boxes_raw = data.get("boxes", [])  #List of "cx cy w h" strings
        focus_crop_idx_raw = data.get("focus_crop_idx")
        focus_crop_idx: Optional[int] = None
        try:
            if focus_crop_idx_raw is not None and str(focus_crop_idx_raw).strip() != "":
                parsed_focus = int(focus_crop_idx_raw)
                if parsed_focus >= 0:
                    focus_crop_idx = parsed_focus
        except Exception:
            focus_crop_idx = None
        serial_i = _parse_serial(str(serial or ""))
        try:
            await V.warm_labeler_refs(force=False)
        except Exception:
            pass
        refs_per_full = max(1, int(getattr(settings, "labeler_ref_per_candidate", 5) or 5))
        refs_per = refs_per_full
        refs_per_target = int(refs_per)
        # Ask for a slightly deeper pool so filtered or broken refs do not leave
        # visible gaps in the UI.
        refs_per_keep = max(refs_per_target, refs_per_target + 4)
        refs_per_query = (
            max(refs_per_keep, int(_IDENTIFY_PREFETCH_REFS_PER_CANDIDATE))
            if prefetch
            else max(refs_per_keep, refs_per_target * 3)
        )
        top_k = max(1, int(getattr(settings, "labeler_top_k", 9) or 9))
        # Serve classify refs through the existing ref-crop URL endpoint backed
        # by local serial/crop metadata instead of inlining thumbs in the hot path.
        include_ref_thumbs = False

        boxes_sig = "|".join(str(b).strip() for b in boxes_raw)
        req_id = _hash_cache_key(
            "identify_req",
            time.time_ns(),
            serial_i or "",
            str(url or "").strip(),
            int(bool(prefetch)),
            int(refs_per_query),
            int(bool(include_ref_thumbs)),
            focus_crop_idx if (focus_crop_idx is not None) else "",
            boxes_sig,
        )[:10]
        trace_identify = _identify_should_trace(prefetch)
        if prefetch:
            now_mono = time.monotonic()
            backoff_left_ms = int(round(max(0.0, float(_identify_prefetch_backoff_until_mono) - now_mono) * 1000.0))
            if backoff_left_ms > 0:
                if trace_identify:
                    log_action(
                        "labeler_identify_prefetch_skipped",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                        f"reason=timeout_backoff; backoff_left_ms={backoff_left_ms}; streak={int(_identify_prefetch_timeout_streak)}",
                    )
                return _with_cors(web.Response(status=429, text="Busy"), request)
        req_t0 = time.perf_counter()
        sem_wait_ms = 0.0
        identify_ms = 0.0
        image_fetch_ms = 0.0
        image_source = "none"
        enrich_ms = 0.0
        metadata_ref_ms = 0.0
        metadata_ref_applied = 0
        metadata_ref_total = 0
        metadata_ref_gallery = 0
        metadata_ref_fallback = 0
        singleflight_wait_ms = 0.0
        payload_for_singleflight: Optional[Dict[str, Any]] = None

        cache_key = _hash_cache_key(
            "identify",
            _IDENTIFY_REF_PIPELINE_VERSION,
            serial_i or "",
            str(url or "").strip(),
            int(bool(rerank)),
            int(top_k),
            int(refs_per_query),
            int(bool(include_ref_thumbs)),
            boxes_sig,
        )
        cached_payload = _cache_get(_identify_result_cache, cache_key)
        if cached_payload is not None:
            if trace_identify:
                stats = _identify_result_ref_stats(list(cached_payload.get("results") or []))
                log_action(
                    "labeler_identify_cache_hit",
                    f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                    (
                        f"boxes={len(boxes_raw)}; top_k={top_k}; refs_per={refs_per_query}; thumbs={int(bool(include_ref_thumbs))}; "
                        f"crops={stats['crops']}; cands={stats['cands']}; "
                        f"with_refs={stats['with_refs']}; zero_refs={stats['zero_refs']}; "
                        f"avg_refs={stats['avg_refs']}; inline_refs={stats['inline_refs']}; "
                        f"url_refs={stats['url_refs']}; inline_cands={stats['inline_cands']}; "
                        f"url_only_cands={stats['url_only_cands']}"
                    ),
                )
            return _with_cors(web.json_response(cached_payload), request)

        singleflight_future, singleflight_owner = await _identify_singleflight_enter(cache_key)
        if not singleflight_owner:
            wait_timeout = min(10.0, _IDENTIFY_PREFETCH_TIMEOUT_SEC) if prefetch else max(75.0, _IDENTIFY_TIMEOUT_SEC + 30.0)
            t_wait = time.perf_counter()
            try:
                shared = await asyncio.wait_for(asyncio.shield(singleflight_future), timeout=wait_timeout)
                singleflight_wait_ms = (time.perf_counter() - t_wait) * 1000.0
                if isinstance(shared, dict) and shared.get("results") is not None:
                    if trace_identify:
                        stats = _identify_result_ref_stats(list(shared.get("results") or []))
                        log_action(
                            "labeler_identify_singleflight_join",
                            f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                            (
                                f"wait_ms={int(round(singleflight_wait_ms))}; "
                                f"crops={stats['crops']}; cands={stats['cands']}; "
                                f"with_refs={stats['with_refs']}; zero_refs={stats['zero_refs']}; "
                                f"avg_refs={stats['avg_refs']}; inline_refs={stats['inline_refs']}; "
                                f"url_refs={stats['url_refs']}; inline_cands={stats['inline_cands']}; "
                                f"url_only_cands={stats['url_only_cands']}"
                            ),
                        )
                    return _with_cors(web.json_response(shared), request)
            except asyncio.TimeoutError:
                singleflight_wait_ms = (time.perf_counter() - t_wait) * 1000.0
                cached_after_wait = _cache_get(_identify_result_cache, cache_key)
                if cached_after_wait is not None:
                    return _with_cors(web.json_response(cached_after_wait), request)
                if trace_identify:
                    log_action(
                        "labeler_identify_singleflight_timeout",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                        f"wait_ms={int(round(singleflight_wait_ms))}; timeout_s={wait_timeout}",
                    )
                return _with_cors(web.Response(status=429, text="Busy"), request)
            except Exception:
                cached_after_wait = _cache_get(_identify_result_cache, cache_key)
                if cached_after_wait is not None:
                    return _with_cors(web.json_response(cached_after_wait), request)
                return _with_cors(web.Response(status=429, text="Busy"), request)

        try:
            t_image_start = time.perf_counter()
            image_bytes = None
            if serial_i is not None:
                try:
                    image_bytes = await labeler_cache.get_cached_image_async(int(serial_i))
                    if image_bytes:
                        image_source = "cache"
                except Exception:
                    image_bytes = None

            if not image_bytes and not url:
                if trace_identify:
                    log_action(
                        "labeler_identify_bad_request",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                        "missing url and serial-cache image",
                    )
                return _with_cors(web.Response(status=400, text="Missing url or serial"), request)

            if not image_bytes:
                image_bytes = await _fetch_image_bytes_for_labeler(
                    serial_i,
                    url,
                    bypass_backoff=True,
                )
                image_source = "fetch"

            if not image_bytes:
                if trace_identify:
                    log_action(
                        "labeler_identify_no_image",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                        f"source={image_source}",
                    )
                return _with_cors(web.Response(status=400, text="No image available"), request)
            image_fetch_ms = (time.perf_counter() - t_image_start) * 1000.0

            boxes: List[Tuple[float, float, float, float]] = []
            for b in boxes_raw:
                try:
                    parts = [float(p) for p in str(b).strip().split()]
                except Exception:
                    continue
                if len(parts) == 4:
                    boxes.append((parts[0], parts[1], parts[2], parts[3]))

            if prefetch and len(boxes) > int(_IDENTIFY_PREFETCH_MAX_BOXES):
                if trace_identify:
                    log_action(
                        "labeler_identify_prefetch_skipped",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                        f"reason=too_many_boxes; parsed_boxes={len(boxes)}; max_boxes={int(_IDENTIFY_PREFETCH_MAX_BOXES)}",
                    )
                return _with_cors(web.Response(status=429, text="Busy"), request)

            if trace_identify:
                log_action(
                    "labeler_identify_start",
                    f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                    (
                        f"raw_boxes={len(boxes_raw)}; parsed_boxes={len(boxes)}; "
                        f"top_k={top_k}; refs_per={refs_per_query}; thumbs={int(bool(include_ref_thumbs))}; "
                        f"rerank={int(bool(rerank))}; "
                        f"focus={focus_crop_idx if focus_crop_idx is not None else -1}; "
                        f"img_src={image_source}; img_ms={int(round(image_fetch_ms))}; "
                        f"sf_wait_ms={int(round(singleflight_wait_ms))}"
                    ),
                )

            # Run identify on provided boxes (normalized cx,cy,w,h).
            acquired = False
            try:
                t_sem_wait = time.perf_counter()
                await _identify_sem.acquire()
                acquired = True
                sem_wait_ms = (time.perf_counter() - t_sem_wait) * 1000.0

                timeout_sec = (_IDENTIFY_PREFETCH_TIMEOUT_SEC if prefetch else _IDENTIFY_TIMEOUT_SEC)
                t_identify = time.perf_counter()
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        V.identify_boxes,
                        image_bytes,
                        boxes,
                        rerank=rerank,
                        top_k=top_k,
                        refs_per=refs_per_query,
                        include_ref_thumbs=include_ref_thumbs,
                        focus_crop_idx=focus_crop_idx,
                        trace_tag=(
                            f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; "
                            f"thumbs={int(bool(include_ref_thumbs))}; focus={focus_crop_idx if focus_crop_idx is not None else -1}"
                            if (not prefetch or trace_identify)
                            else None
                        ),
                    ),
                    timeout=timeout_sec,
                )
                identify_ms = (time.perf_counter() - t_identify) * 1000.0
                if prefetch:
                    _identify_prefetch_timeout_streak = max(0, int(_identify_prefetch_timeout_streak) - 1)
                    if _identify_prefetch_timeout_streak <= 0:
                        _identify_prefetch_backoff_until_mono = 0.0
            except asyncio.TimeoutError:
                if prefetch:
                    _identify_prefetch_timeout_streak = min(3, int(_identify_prefetch_timeout_streak) + 1)
                    backoff_sec = min(
                        8.0,
                        1.0 * float(2 ** max(0, int(_identify_prefetch_timeout_streak) - 1)),
                    )
                    _identify_prefetch_backoff_until_mono = time.monotonic() + backoff_sec
                log_action("labeler_identify_timeout", f"serial={serial}", f"prefetch={prefetch}")
                if trace_identify or not prefetch:
                    log_action(
                        "labeler_identify_timeout_trace",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                        (
                            f"sem_wait_ms={int(round(sem_wait_ms))}; "
                            f"timeout_s={timeout_sec}; img_ms={int(round(image_fetch_ms))}; "
                            f"boxes={len(boxes)}; top_k={top_k}; refs_per={refs_per_query}; "
                            f"thumbs={int(bool(include_ref_thumbs))}; focus={focus_crop_idx if focus_crop_idx is not None else -1}"
                        ),
                    )
                return _with_cors(web.Response(status=504, text="Identify timed out"), request)
            finally:
                if acquired:
                    _identify_sem.release()

            # Enrich candidates with physical descriptions from local cache (if available)
            try:
                t_enrich = time.perf_counter()
                from ..services import profile_cache
                for crop in result.results:
                    for cand in crop.get("candidates", []) or []:
                        name = cand.get("name")
                        if not name:
                            continue
                        prof = profile_cache.get_profile_local(str(name))
                        if not prof:
                            continue
                        desc = prof.get("physical_description") or prof.get("physical")
                        if desc:
                            cand["desc"] = str(desc)
                enrich_ms = (time.perf_counter() - t_enrich) * 1000.0
            except Exception:
                pass

            # Keep refs strictly query-specific from DINO similarity results.
            # For each predicted cat, use only its DINO-ranked refs and map them
            # to ref-crop endpoints; no deterministic metadata fallback override.
            try:
                t_metadata_refs = time.perf_counter()
                await _ensure_photo_crop_index_cache(force=False)

                for crop in [row for row in result.results if isinstance(row, dict)]:
                    for cand in crop.get("candidates", []) or []:
                        refs_raw = cand.get("refs")
                        ref_target = max(1, int(refs_per_target))
                        ref_keep_limit = max(ref_target, int(refs_per_keep))
                        refs_meta = list(refs_raw) if isinstance(refs_raw, list) else []
                        selected_refs: List[Dict[str, Any]] = []
                        seen_sc: Set[Tuple[int, int]] = set()
                        seen_img: Set[str] = set()
                        for row in refs_meta:
                            if not isinstance(row, dict):
                                continue
                            img_b64 = str(row.get("img") or "").strip()
                            if img_b64 and img_b64 in seen_img:
                                continue
                            serial_ref: Optional[int] = None
                            crop_ref: Optional[int] = None
                            try:
                                serial_ref = int(row.get("serial"))
                                crop_ref = int(row.get("crop"))
                            except Exception:
                                serial_ref = None
                                crop_ref = None
                            if serial_ref is None or crop_ref is None or serial_ref <= 0 or crop_ref <= 0:
                                # Keep DINO-ranked inline refs even if serial/crop metadata is missing.
                                if not img_b64:
                                    continue
                                selected_refs.append({
                                    "img": img_b64,
                                    "url": "",
                                    "serial": None,
                                    "crop": None,
                                    "source": "dino_gallery_inline",
                                })
                                seen_img.add(img_b64)
                                if len(selected_refs) >= ref_keep_limit:
                                    break
                                continue
                            if _is_flagged_ref_serial(int(serial_ref)):
                                continue
                            key_sc = (serial_ref, crop_ref)
                            if key_sc in seen_sc:
                                continue
                            entry = _photo_crop_index_cache.get((int(serial_ref), int(crop_ref))) or {}
                            if not entry:
                                # Skip refs whose metadata entry was removed so
                                # the gallery does not resurrect stale thumbnails.
                                continue
                            seen_sc.add(key_sc)
                            ref_url = _photo_ref_crop_url(serial_ref, crop_ref)
                            selected_refs.append({
                                "img": img_b64,
                                "url": ref_url,
                                "serial": serial_ref,
                                "crop": crop_ref,
                                "source": "dino_gallery",
                            })
                            if img_b64:
                                seen_img.add(img_b64)
                            if len(selected_refs) >= ref_keep_limit:
                                break

                        selected_refs, fallback_added = _supplement_candidate_refs_with_fallback(
                            selected_refs,
                            cat_name=str(cand.get("name") or ""),
                            ref_target=ref_target,
                            ref_keep_limit=ref_keep_limit,
                            seen_sc=seen_sc,
                            prefer_serial=serial_i,
                        )
                        cand["refs"] = selected_refs
                        if cand["refs"]:
                            metadata_ref_applied += 1
                        metadata_ref_total += len(cand["refs"])
                        metadata_ref_gallery += int(len(cand["refs"]))
                        metadata_ref_fallback += int(fallback_added)

                metadata_ref_ms = (time.perf_counter() - t_metadata_refs) * 1000.0
            except Exception as e:
                log_action(
                    "labeler_identify_metadata_refs_error",
                    "error",
                    f"{type(e).__name__}: {e!r}",
                )

            payload = {"results": result.results}
            payload_for_singleflight = payload
            _cache_set(
                _identify_result_cache,
                cache_key,
                payload,
                max_items=_IDENTIFY_RESULT_CACHE_MAX,
            )
            _schedule_ref_crop_warm(
                _collect_identify_result_ref_pairs(
                    list(result.results or []),
                    max_candidates=_CLASSIFY_REF_CROP_WARM_MAX_CANDIDATES,
                    max_refs_per_candidate=_CLASSIFY_REF_CROP_WARM_MAX_REFS,
                ),
                thumb_size=_REF_CROP_WARM_SIZE,
                force=False,
            )
            if trace_identify:
                stats = _identify_result_ref_stats(list(result.results or []))
                total_ms = (time.perf_counter() - req_t0) * 1000.0
                log_action(
                    "labeler_identify_done",
                    f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                    (
                        f"ms total={int(round(total_ms))} img={int(round(image_fetch_ms))} "
                        f"sem={int(round(sem_wait_ms))} id={int(round(identify_ms))} "
                        f"enrich={int(round(enrich_ms))} metadata_refs={int(round(metadata_ref_ms))}; "
                        f"src={image_source}; refs_per={refs_per_query}; thumbs={int(bool(include_ref_thumbs))}; "
                        f"crops={stats['crops']}; cands={stats['cands']}; "
                        f"with_refs={stats['with_refs']}; zero_refs={stats['zero_refs']}; "
                        f"avg_refs={stats['avg_refs']}; max_refs={stats['max_refs']}; "
                        f"inline_refs={stats['inline_refs']}; url_refs={stats['url_refs']}; "
                        f"inline_cands={stats['inline_cands']}; url_only_cands={stats['url_only_cands']}; "
                        f"metadata_applied={int(metadata_ref_applied)}; metadata_total={int(metadata_ref_total)}; "
                        f"metadata_gallery={int(metadata_ref_gallery)}; metadata_fallback={int(metadata_ref_fallback)}; "
                        f"sf_wait={int(round(singleflight_wait_ms))}"
                    ),
                )
            return _with_cors(web.json_response(payload), request)
        finally:
            if singleflight_owner:
                await _identify_singleflight_finish(cache_key, singleflight_future, payload_for_singleflight)
    except Exception as e:
        log_action("labeler_identify_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


async def post_manual_candidates(request: web.Request) -> web.Response:
    """Return all-cat manual-review candidates for one crop."""
    try:
        data = await request.json()
        serial = data.get("serial")
        url = data.get("url")
        serial_i = _parse_serial(str(serial or ""))

        box_raw = str(data.get("box") or "").strip()
        if not box_raw:
            boxes = data.get("boxes", []) or []
            try:
                crop_idx = int(data.get("crop_idx", 0) or 0)
            except Exception:
                crop_idx = 0
            if 0 <= crop_idx < len(boxes):
                box_raw = str(boxes[crop_idx] or "").strip()
        box = _parse_yolo_box_str(box_raw)
        if box is None:
            return _with_cors(web.Response(status=400, text="Missing valid box"), request)

        cache_key = _hash_cache_key(
            "manual_candidates",
            _MANUAL_CANDIDATE_PIPELINE_VERSION,
            serial_i or "",
            str(url or "").strip(),
            box_raw,
            int(_MANUAL_QUERY_REFS_PER_CAT),
            int(_MANUAL_QUERY_REF_CAT_LIMIT),
            int(_MANUAL_QUERY_REF_SEARCH_POOL),
            int(_MANUAL_FALLBACK_REFS_PER_CAT),
        )
        cached_payload = _cache_get(_manual_result_cache, cache_key)
        if cached_payload is not None:
            return _with_cors(web.json_response(cached_payload), request)

        # Warm lightweight manual-review state in background, but do not block.
        try:
            await V.warm_labeler_manual_refs(force=False)
        except Exception:
            pass

        image_bytes = None
        if serial_i is not None:
            try:
                image_bytes = await labeler_cache.get_cached_image_async(int(serial_i))
            except Exception:
                image_bytes = None

        if not image_bytes and not url:
            return _with_cors(web.Response(status=400, text="Missing url or serial"), request)

        if not image_bytes:
            image_bytes = await _fetch_image_bytes_for_labeler(serial_i, url)

        if not image_bytes:
            return _with_cors(web.Response(status=400, text="No image available"), request)

        raw_candidates: List[Dict[str, Any]] = []
        acquired = False
        try:
            await _manual_sem.acquire()
            acquired = True
            raw_candidates = await asyncio.wait_for(
                asyncio.to_thread(
                    V.manual_review_candidates,
                    image_bytes,
                    box,
                    refs_per=_MANUAL_QUERY_REFS_PER_CAT,
                    query_ref_cat_limit=_MANUAL_QUERY_REF_CAT_LIMIT,
                    gallery_ref_search_pool=_MANUAL_QUERY_REF_SEARCH_POOL,
                    rerank=False,
                ),
                timeout=_MANUAL_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log_action("labeler_manual_candidates_timeout", f"serial={serial}", "manual_review")
            return _with_cors(web.Response(status=504, text="Manual candidates timed out"), request)
        except Exception as e:
            # Fail soft: still return full CatDatabase list + fallback refs.
            log_action(
                "labeler_manual_candidates_score_error",
                f"serial={serial}",
                f"{type(e).__name__}: {e!r}",
            )
            raw_candidates = []
        finally:
            if acquired:
                _manual_sem.release()

        alias_lookup, ordered_profile, _ = await asyncio.to_thread(_load_profile_catalog)
        try:
            await _ensure_manual_metadata_ref_cache(alias_lookup, force=False)
            await _ensure_photo_crop_index_cache(force=False)
        except Exception as e:
            # Keep manual mode usable even if metadata reference sampling fails.
            log_action(
                "labeler_manual_metadata_ref_cache_error",
                "error",
                f"{type(e).__name__}: {e!r}",
            )

        candidates = await asyncio.to_thread(
            _build_manual_candidate_catalog,
            raw_candidates or [],
            alias_lookup=alias_lookup,
            ordered_profile=ordered_profile,
            prefer_serial=serial_i,
        )
        total_known = len(ordered_profile) if ordered_profile else len(candidates)
        status = _manual_ref_cache_status_payload(total_hint=total_known)

        payload = {
            "ready": True,
            "cache_status": status,
            "candidates": candidates,
        }
        _cache_set(
            _manual_result_cache,
            cache_key,
            payload,
            max_items=_MANUAL_RESULT_CACHE_MAX,
        )
        _schedule_ref_crop_warm(
            _collect_manual_candidate_ref_pairs(
                candidates,
                max_candidates=_MANUAL_REF_CROP_WARM_MAX_CANDIDATES,
                max_refs_per_candidate=_MANUAL_REF_CROP_WARM_MAX_REFS,
            ),
            thumb_size=_REF_CROP_WARM_SIZE,
            force=False,
        )
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_manual_candidates_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


#---------- Save Endpoint ----------

def _apply_save_updates_sync(updates: List[Dict[str, Any]], actor_name: str) -> Dict[str, Any]:
    """Apply annotation updates to the local metadata CSV.

    Skips the synchronous cache refresh here because post_save already
    fires _kickoff_photo_metadata_cache_refresh as a non-blocking
    background task, avoiding redundant I/O that can push remote
    clients past the 90 s request timeout.
    """
    return local_photos.update_metadata_annotations(updates, actor_name, refresh=False)


async def post_save(request: web.Request) -> web.Response:
    """Batch save annotations to the local metadata CSV."""
    try:
        global _manual_metadata_ref_cache, _manual_metadata_ref_built_mono
        global _photo_crop_index_cache, _photo_crop_index_built_mono
        data = await request.json()
        if not isinstance(data, dict):
            return _with_cors(web.Response(status=400, text="Invalid request"), request)
        updates = data.get("updates", [])  #List of {serial, box_coords, box_cat_ids}
        _, actor_name = _actor_from_request(request)

        if not isinstance(updates, list) or not updates:
            return _with_cors(web.Response(status=400, text="No updates"), request)
        if len(updates) > _SAVE_BATCH_MAX:
            return _with_cors(
                web.Response(status=400, text=f"Too many updates (maximum {_SAVE_BATCH_MAX})"),
                request,
            )

        save_outcome = await asyncio.to_thread(_apply_save_updates_sync, updates, str(actor_name or ""))
        pending_unblacklist_ref_serials = list(save_outcome.get("pending_unblacklist_ref_serials") or [])
        cleared_ref_blacklist_serials: List[int] = []

        for sn in pending_unblacklist_ref_serials:
            if _discard_flagged_ref_serial(int(sn)):
                cleared_ref_blacklist_serials.append(int(sn))
        if cleared_ref_blacklist_serials:
            try:
                await _remove_flag_incorrect_queue_serials(cleared_ref_blacklist_serials)
            except Exception as e:
                log_action("labeler_flag_incorrect_queue_drop_error", "error", f"{type(e).__name__}: {e!r}")

    # Refresh local photo metadata cache in background to avoid blocking the event loop
        # (gspread/requests is synchronous and can stall Discord heartbeats).
        _kickoff_photo_metadata_cache_refresh("post_save")
        _manual_metadata_ref_cache = {}
        _manual_metadata_ref_built_mono = 0.0
        _photo_crop_index_cache = {}
        _photo_crop_index_built_mono = 0.0
        #Saving happens constantly while labeling. Clearing every rendered ref
        #crop each time forced the UI (which fetches ref crops 12 at a time) to
        #re-render the whole gallery from disk after every save, so evict only
        #the serials this save actually touched.
        saved_serials = [int(sn) for sn in save_outcome.get("saved_serials", [])]
        _drop_ref_crop_renders_for_serials(saved_serials)

        saved_count = int(save_outcome.get("saved", len(saved_serials)) or 0)
        requested_count = int(save_outcome.get("requested", len(updates)) or 0)
        missing_serials = [int(sn) for sn in save_outcome.get("missing_serials", [])]
        log_action(
            "labeler_save",
            "saved",
            f"saved={saved_count}; requested={requested_count}; missing={len(missing_serials)}; actor={actor_name}",
        )
        return _with_cors(web.json_response({
            "status": "ok",
            "requested": requested_count,
            "saved": saved_count,
            "saved_serials": saved_serials,
            "missing_serials": missing_serials,
            "unblacklisted_ref_serials": sorted(list(dict.fromkeys(cleared_ref_blacklist_serials))),
        }), request)
    except local_photos.AnnotationUpdateError as e:
        log_action("labeler_save_error", "bad_request", str(e))
        return _with_cors(web.Response(status=400, text=str(e)), request)
    except ValueError as e:
        log_action("labeler_save_error", "bad_request", str(e))
        return _with_cors(web.Response(status=400, text="Invalid request"), request)
    except Exception as e:
        log_action("labeler_save_error", "error", str(e))
        return _internal_error_response(request)


async def post_flag_incorrect(request: web.Request) -> web.Response:
    """Blacklist a reference serial immediately and queue metadata label clears in background."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    serial = _parse_serial(str(data.get("serial") or ""))
    if serial is None:
        return _with_cors(web.Response(status=400, text="Missing serial"), request)

    source_mode = str(data.get("source_mode") or "").strip().lower()
    source_serial = _parse_serial(str(data.get("source_serial") or ""))
    source_crop_raw = data.get("source_crop")
    try:
        source_crop = int(source_crop_raw) if source_crop_raw is not None else None
    except Exception:
        source_crop = None
    _, actor_name = _actor_from_request(request)

    try:
        blacklisted_now = _add_flagged_ref_serial(int(serial))
        queue_info = await _enqueue_flag_incorrect_job(
            int(serial),
            actor_name=actor_name,
            source_mode=source_mode,
            source_serial=source_serial,
            source_crop=source_crop,
        )
        # Clear stale ref payloads so blacklist changes take effect on the next request.
        _invalidate_labeler_caches_after_label_clears([int(serial)])

        src_parts: List[str] = []
        if source_mode:
            src_parts.append(f"mode={source_mode}")
        if source_serial is not None:
            src_parts.append(f"source_sn={int(source_serial)}")
        if source_crop is not None:
            src_parts.append(f"source_crop={int(source_crop)}")
        src_txt = ";".join(src_parts) if src_parts else "mode=unknown"
        log_action(
            "labeler_flag_incorrect",
            f"sn={int(serial)}; queued=1; new={1 if queue_info.get('queued_new') else 0}; blacklisted={1 if blacklisted_now else 0}",
            f"by={actor_name}; pending={int(queue_info.get('pending_count') or 0)}; {src_txt}",
        )

        return _with_cors(
            web.json_response(
                {
                    "status": "ok",
                    "serial": int(serial),
                    "queued": True,
                    "applied": False,
                    "sheet_update": "queued",
                    "queue_pending": int(queue_info.get("pending_count") or 0),
                    "queue_deduped": not bool(queue_info.get("queued_new")),
                    # Background metadata clearing means the immediate changed-state is unknown.
                    "changed": None,
                    "already_unlabeled": None,
                    "blacklisted_for_refs": True,
                }
            ),
            request,
        )
    except Exception as e:
        log_action("labeler_flag_incorrect_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


#---------- Cat List Endpoint ----------

async def get_cats(request: web.Request) -> web.Response:
    """Return all profile and gallery identities available to the labeler."""
    try:
        def load_names() -> List[str]:
            _, profile_rows, _ = _load_profile_catalog()
            names = [str(row.get("name") or "").strip() for row in profile_rows]
            names.extend(str(name or "").strip() for name in (V.get_all_cats() or []))
            return list(dict.fromkeys(name for name in names if name))

        cats = await asyncio.to_thread(load_names)
        return _with_cors(web.json_response({"cats": cats}), request)
    except Exception as e:
        log_action("labeler_get_cats_error", "error", str(e))
        return _internal_error_response(request)


#---------- Reference Cache Endpoints ----------

async def post_refs_warm(request: web.Request) -> web.Response:
    """Warm the per-cat reference cache for classifier refs."""
    try:
        force = False
        try:
            body = await request.json()
            force = bool(body.get("force"))
        except Exception:
            force = False
        status = await V.warm_labeler_refs(force=force)
        started = _schedule_classifier_ref_crop_warm(
            refs_per_cat=_CLASSIFY_REF_CROP_WARM_MAX_REFS,
            thumb_size=_REF_CROP_WARM_SIZE,
            force=force,
        )
        status = dict(status or {})
        status["ref_crop_size"] = int(_REF_CROP_WARM_SIZE)
        status["ref_crop_warm_started"] = bool(started)
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("labeler_refs_warm_error", "error", str(e))
        return _internal_error_response(request)


async def get_refs_status(request: web.Request) -> web.Response:
    """Get reference cache status."""
    try:
        return _with_cors(web.json_response(V.labeler_ref_status()), request)
    except Exception as e:
        log_action("labeler_refs_status_error", "error", str(e))
        return _internal_error_response(request)


async def post_manual_refs_warm(request: web.Request) -> web.Response:
    """Warm manual-review metadata refs + crop index + classifier state."""
    try:
        force = False
        try:
            body = await request.json()
            force = bool(body.get("force"))
        except Exception:
            force = False
        alias_lookup, ordered_profile, _ = await asyncio.to_thread(_load_profile_catalog)
        await asyncio.gather(
            V.warm_labeler_refs(force=force),
            V.warm_labeler_manual_refs(force=force),
            _ensure_manual_metadata_ref_cache(alias_lookup, force=force),
            _ensure_photo_crop_index_cache(force=force),
        )
        status = _manual_ref_cache_status_payload(total_hint=len(ordered_profile))
        queued = _schedule_ref_crop_warm(
            _collect_manual_metadata_ref_pairs(
                refs_per_cat=_MANUAL_FALLBACK_REFS_PER_CAT,
            ),
            thumb_size=_REF_CROP_WARM_SIZE,
            force=force,
        )
        started = _schedule_classifier_ref_crop_warm(
            refs_per_cat=_CLASSIFY_REF_CROP_WARM_MAX_REFS,
            thumb_size=_REF_CROP_WARM_SIZE,
            force=force,
        )
        status["ref_crop_size"] = int(_REF_CROP_WARM_SIZE)
        status["ref_crop_queued"] = int(queued)
        status["ref_crop_warm_started"] = bool(started)
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("labeler_manual_refs_warm_error", "error", str(e))
        return _internal_error_response(request)


async def get_manual_refs_status(request: web.Request) -> web.Response:
    """Get manual-review metadata-ref cache status."""
    try:
        return _with_cors(web.json_response(_manual_ref_cache_status_payload()), request)
    except Exception as e:
        log_action("labeler_manual_refs_status_error", "error", str(e))
        return _internal_error_response(request)


async def get_gallery_retrain_status_api(request: web.Request) -> web.Response:
    """Read current 4AM gallery retrain schedule/status."""
    try:
        status = await get_gallery_retrain_status()
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("gallery_retrain_status_error", "error", str(e))
        return _internal_error_response(request)


async def post_gallery_retrain_schedule_api(request: web.Request) -> web.Response:
    """Schedule the next 4AM full gallery retrain run."""
    try:
        user_id, actor_name = _actor_from_request(request)
        requester = actor_name if actor_name else (user_id or "unknown")
        status = await schedule_gallery_retrain(requested_by=requester)
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("gallery_retrain_schedule_error", "error", str(e))
        return _internal_error_response(request)


async def post_cache_warm(request: web.Request) -> web.Response:
    """Queue a batch cache warm for labeler source images."""
    try:
        if not _BATCH_WARM_ENABLE:
            return _with_cors(
                web.json_response({"accepted": False, "queued": 0, "in_flight": _labeler_cache_inflight_count(), "cache_target": 0}),
                request,
            )
        try:
            data = await request.json()
        except Exception:
            data = {}
        mode = str(data.get("mode") or "boot").strip().lower()
        if mode not in {"detect", "classify", "manual", "boot"}:
            mode = "boot"
        raw_items = data.get("items")
        scan_limit = int(data.get("scan_limit") or _QUEUE_CACHE_WARM_SCAN_LIMIT)
        scan_limit = max(10, min(scan_limit, 5000))
        target_raw = int(data.get("target_count") or 0)
        priority = str(data.get("priority") or "normal").strip().lower()
        if priority not in {"low", "normal", "high"}:
            priority = "normal"

        items: List[Dict[str, Any]] = []
        if isinstance(raw_items, list) and raw_items:
            seen: Set[int] = set()
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                try:
                    sn = int(it.get("serial") or 0)
                except Exception:
                    sn = 0
                url = str(it.get("url") or "").strip()
                if sn <= 0 or sn in seen:
                    continue
                seen.add(sn)
                items.append({"serial": sn, "url": url})
                if len(items) >= scan_limit:
                    break
        else:
            rows = await _get_photo_metadata_rows_async(ttl_sec=120)
            items = _queue_items_from_rows(rows, mode=mode, max_items=scan_limit)

        if not items:
            return _with_cors(
                web.json_response(
                    {
                        "accepted": True,
                        "queued": 0,
                        "in_flight": _labeler_cache_inflight_count(),
                        "cache_target": 0,
                    }
                ),
                request,
            )

        if target_raw > 0:
            target = min(target_raw, len(items))
        elif mode == "boot":
            target = min(_labeler_cache_target_from_budget(float(_BOOT_WARM_BUDGET_GB)), len(items))
        else:
            target = min(int(_QUEUE_CACHE_WARM_TARGET), len(items))

        concurrency = int(_BOOT_WARM_CONCURRENCY)
        if priority == "high":
            concurrency = max(concurrency, 6)
        elif priority == "low":
            concurrency = max(1, min(concurrency, 2))

        async def _runner() -> None:
            try:
                warm_scan_limit = min(scan_limit, len(items))
                await asyncio.gather(
                    _refresh_local_photo_cache_state(
                        items,
                        target_count=target,
                        scan_limit=warm_scan_limit,
                        concurrency=max(1, concurrency),
                    ),
                    _warm_item_context_cache_for_items(
                        items[:warm_scan_limit],
                        force_raw=False,
                    ),
                    return_exceptions=True,
                )
            except Exception:
                pass

        asyncio.create_task(_runner())
        return _with_cors(
            web.json_response(
                {
                    "accepted": True,
                    "queued": int(len(items)),
                    "in_flight": _labeler_cache_inflight_count(),
                    "cache_target": int(target),
                }
            ),
            request,
        )
    except Exception as e:
        log_action("labeler_cache_warm_error", "error", f"{type(e).__name__}: {e!r}")
        return _internal_error_response(request)


#---------- OPTIONS handlers for CORS ----------

async def options_handler(request: web.Request) -> web.Response:
    """Handle CORS preflight requests."""
    resp = web.Response(status=204)
    return _with_cors(resp, request)


#---------- Route registration ----------

async def kickoff_boot_cache_warm_startup() -> None:
    """Restore legacy labels, then fire-and-forget boot cache warming."""
    try:
        from ..services.legacy_wildlife import restore_legacy_wildlife_annotations

        restored = await asyncio.to_thread(restore_legacy_wildlife_annotations)
        count = int(restored.get("restored") or 0)
        if count:
            log_action(
                "legacy_wildlife_restored",
                f"restored={count}",
                ",".join(str(sn) for sn in restored.get("restored_serials") or []),
            )
        gallery_names = {str(name or "").strip() for name in (V.get_all_cats() or [])}
        needs_r7 = not {"Melvin", "Stove"}.issubset(gallery_names)
        if needs_r7 and str(getattr(settings, "cv_backend", "") or "").strip().lower() == "modal":
            scheduled = await schedule_gallery_retrain(
                requested_by="legacy-wildlife-v1",
                gallery_version="7",
                required_backend="modal",
            )
            log_action(
                "legacy_wildlife_gallery_scheduled",
                "version=R7 backend=modal",
                str(scheduled.get("scheduled_date") or ""),
            )
        elif needs_r7:
            log_action(
                "legacy_wildlife_gallery_deferred",
                "version=R7 backend=modal",
                f"active_backend={getattr(settings, 'cv_backend', '')}",
            )
    except Exception as e:
        log_action("legacy_wildlife_restore_error", "error", f"{type(e).__name__}: {e!r}")
    await _log_local_mode_once()
    _kickoff_boot_cache_warm_once()


def get_labeler_routes() -> List:
    """Return list of labeler API routes for registration in main.py."""
    return [
        web.get("/api/labeler/queue/detect", get_queue_detect),
        web.get("/api/labeler/queue/classify", get_queue_classify),
        web.get("/api/labeler/queue/manual", get_queue_manual),
        web.post("/api/labeler/claim", post_claim),
        web.get("/api/labeler/image/{sn}", get_image),
        web.get("/api/labeler/context/{sn}", get_item_context),
        web.get("/api/labeler/cached_image/{sn}", get_cached_image),
        web.get("/api/labeler/ref_crop/{sn}/{crop}", get_ref_crop),
        web.post("/api/labeler/detect", post_detect),
        web.post("/api/labeler/refine", post_refine),
        web.post("/api/labeler/identify", post_identify),
        web.post("/api/labeler/ui_diag", post_ui_diag),
        web.post("/api/labeler/manual/candidates", post_manual_candidates),
        web.post("/api/labeler/save", post_save),
        web.post("/api/labeler/flag_incorrect", post_flag_incorrect),
        web.get("/api/labeler/cats", get_cats),
        web.post("/api/labeler/refs/warm", post_refs_warm),
        web.get("/api/labeler/refs/status", get_refs_status),
        web.post("/api/labeler/manual_refs/warm", post_manual_refs_warm),
        web.get("/api/labeler/manual_refs/status", get_manual_refs_status),
        web.post("/api/labeler/cache/warm", post_cache_warm),
        web.get("/api/labeler/gallery_retrain/status", get_gallery_retrain_status_api),
        web.post("/api/labeler/gallery_retrain/schedule", post_gallery_retrain_schedule_api),
        #CORS preflight
        web.options("/api/labeler/queue/detect", options_handler),
        web.options("/api/labeler/queue/classify", options_handler),
        web.options("/api/labeler/queue/manual", options_handler),
        web.options("/api/labeler/claim", options_handler),
        web.options("/api/labeler/image/{sn}", options_handler),
        web.options("/api/labeler/context/{sn}", options_handler),
        web.options("/api/labeler/cached_image/{sn}", options_handler),
        web.options("/api/labeler/ref_crop/{sn}/{crop}", options_handler),
        web.options("/api/labeler/detect", options_handler),
        web.options("/api/labeler/refine", options_handler),
        web.options("/api/labeler/identify", options_handler),
        web.options("/api/labeler/ui_diag", options_handler),
        web.options("/api/labeler/manual/candidates", options_handler),
        web.options("/api/labeler/save", options_handler),
        web.options("/api/labeler/flag_incorrect", options_handler),
        web.options("/api/labeler/cats", options_handler),
        web.options("/api/labeler/refs/warm", options_handler),
        web.options("/api/labeler/refs/status", options_handler),
        web.options("/api/labeler/manual_refs/warm", options_handler),
        web.options("/api/labeler/manual_refs/status", options_handler),
        web.options("/api/labeler/cache/warm", options_handler),
        web.options("/api/labeler/gallery_retrain/status", options_handler),
        web.options("/api/labeler/gallery_retrain/schedule", options_handler),
    ]

