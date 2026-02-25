"""API endpoints for the web-based image labeling tool.

Routes:
  GET  /api/labeler/queue/detect    - Serials needing detector labels
  GET  /api/labeler/queue/classify  - Serials needing classifier labels
  GET  /api/labeler/image/<sn>      - Image + existing annotations
  GET  /api/labeler/cached_image/<sn> - Cached image bytes (fast)
  POST /api/labeler/detect          - Run YOLO+SAM → boxes
  POST /api/labeler/identify        - Run DINOv3 → top-N candidates
  POST /api/labeler/save            - Batch save annotations to sheet
  POST /api/labeler/flag_incorrect  - Clear labels for one serial for relabel
  GET  /api/labeler/cats            - List all cat names for dropdown
"""
from __future__ import annotations
import io
import re
import os
import time
import random
import hashlib
import base64
import asyncio
import aiohttp
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

from aiohttp import web
from PIL import Image

from ..config import settings
from ..logger import log_action
from ..vision import vision as V
from ..services.catsheets import get_tcb_pics_rows, force_refresh_tcb_cache
from ..services.sheets_client import sheets_client
from ..services import labeler_cache
from ..services.gallery_retrain import get_gallery_retrain_status, schedule_gallery_retrain

#Column indices in TCB Pics Formatted (0-indexed)
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
_MANUAL_CONCURRENCY = max(1, int(os.getenv("LABELER_MANUAL_CONCURRENCY", "1") or "1"))
_MANUAL_TIMEOUT_SEC = float(os.getenv("LABELER_MANUAL_TIMEOUT_SEC", "60") or "60")
_DETECT_CONCURRENCY = max(1, int(os.getenv("LABELER_DETECT_CONCURRENCY", "2") or "2"))
_REFINE_CONCURRENCY = max(1, int(os.getenv("LABELER_REFINE_CONCURRENCY", "2") or "2"))
_DETECT_TIMEOUT_SEC = float(os.getenv("LABELER_DETECT_TIMEOUT_SEC", "25") or "25")
_DETECT_PREFETCH_TIMEOUT_SEC = float(os.getenv("LABELER_DETECT_PREFETCH_TIMEOUT_SEC", "15") or "15")
_REFINE_TIMEOUT_SEC = float(os.getenv("LABELER_REFINE_TIMEOUT_SEC", "25") or "25")
_REFINE_PREFETCH_TIMEOUT_SEC = float(os.getenv("LABELER_REFINE_PREFETCH_TIMEOUT_SEC", "15") or "15")
_DETECT_INLINE_SAM_TIMEOUT_SEC = float(
    os.getenv("LABELER_DETECT_INLINE_SAM_TIMEOUT_SEC", "8") or "8"
)
_identify_sem = asyncio.Semaphore(_IDENTIFY_CONCURRENCY)
_manual_sem = asyncio.Semaphore(_MANUAL_CONCURRENCY)
_detect_sem = asyncio.Semaphore(_DETECT_CONCURRENCY)
_refine_sem = asyncio.Semaphore(_REFINE_CONCURRENCY)
_CV_RESULT_TTL_SEC = max(5.0, float(os.getenv("LABELER_CV_RESULT_TTL_SEC", "180") or "180"))
_DETECT_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_DETECT_RESULT_CACHE_MAX", "1000") or "1000"))
_REFINE_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_REFINE_RESULT_CACHE_MAX", "1200") or "1200"))
_IDENTIFY_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_IDENTIFY_RESULT_CACHE_MAX", "900") or "900"))
_MANUAL_RESULT_CACHE_MAX = max(50, int(os.getenv("LABELER_MANUAL_RESULT_CACHE_MAX", "600") or "600"))
_CLASSIFY_MIN_PIXELS = max(0, int(os.getenv("LABELER_CLASSIFY_MIN_PIXELS", "122500") or "122500"))
_CLASSIFY_MIN_DIM = max(0, int(os.getenv("LABELER_CLASSIFY_MIN_DIM", "0") or "0"))
_CLASSIFY_MIN_BLUR = max(0.0, float(os.getenv("LABELER_CLASSIFY_MIN_BLUR", "35") or "35"))
_CLASSIFY_BLUR_MAX_DIM = max(64, int(os.getenv("LABELER_CLASSIFY_BLUR_MAX_DIM", "640") or "640"))
_CLASSIFY_PREFILTER_MAX_SYNC_ITEMS = max(
    0,
    int(os.getenv("LABELER_CLASSIFY_PREFILTER_MAX_SYNC_ITEMS", "8") or "8"),
)
_CLASSIFY_PREFILTER_SYNC_ITEM_TIMEOUT_SEC = max(
    0.5,
    float(os.getenv("LABELER_CLASSIFY_PREFILTER_SYNC_ITEM_TIMEOUT_SEC", "3") or "3"),
)
_CLASSIFY_PREFILTER_BG_CONCURRENCY = max(
    1,
    min(10, int(os.getenv("LABELER_CLASSIFY_PREFILTER_BG_CONCURRENCY", "3") or "3")),
)
_CLASSIFY_PREFILTER_CACHE_TTL_SEC = max(
    30.0,
    float(os.getenv("LABELER_CLASSIFY_PREFILTER_CACHE_TTL_SEC", "1800") or "1800"),
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
_classify_quality_cache: Dict[int, Tuple[float, bool, int, int, float]] = {}
_auto_reject_quality_inflight: set[int] = set()
_classify_quality_scan_inflight: set[int] = set()
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
_MANUAL_SHEET_REF_SAMPLE_PER_CAT = max(
    _MANUAL_FALLBACK_REFS_PER_CAT,
    int(os.getenv("LABELER_MANUAL_SHEET_REF_SAMPLE_PER_CAT", "20") or "20"),
)
_MANUAL_SHEET_REF_CROPPED_SAMPLE_PER_CAT = max(
    _MANUAL_SHEET_REF_SAMPLE_PER_CAT,
    int(os.getenv("LABELER_MANUAL_SHEET_REF_CROPPED_SAMPLE_PER_CAT", "40") or "40"),
)
_MANUAL_SHEET_REF_UNCROPPED_SAMPLE_PER_CAT = max(
    1,
    int(os.getenv("LABELER_MANUAL_SHEET_REF_UNCROPPED_SAMPLE_PER_CAT", "8") or "8"),
)
_MANUAL_SHEET_REF_TTL_SEC = max(30, int(os.getenv("LABELER_MANUAL_SHEET_REF_TTL_SEC", "600") or "600"))
_IDENTIFY_FALLBACK_PREFETCH = str(
    os.getenv("LABELER_IDENTIFY_FALLBACK_PREFETCH", "0")
).strip().lower() in {"1", "true", "yes", "on"}
_IDENTIFY_FALLBACK_SCAN_MULT = max(
    1,
    min(12, int(os.getenv("LABELER_IDENTIFY_FALLBACK_SCAN_MULT", "4") or "4")),
)
_IDENTIFY_FALLBACK_REMOTE_MAX_TOTAL = max(
    0,
    int(os.getenv("LABELER_IDENTIFY_FALLBACK_REMOTE_MAX_TOTAL", "48") or "48"),
)
_IDENTIFY_FALLBACK_REMOTE_MAX_PER_CAND = max(
    0,
    int(os.getenv("LABELER_IDENTIFY_FALLBACK_REMOTE_MAX_PER_CAND", "5") or "5"),
)
_IDENTIFY_FALLBACK_REMOTE_TIMEOUT_SEC = max(
    0.2,
    float(os.getenv("LABELER_IDENTIFY_FALLBACK_REMOTE_TIMEOUT_SEC", "0.6") or "0.6"),
)
_IDENTIFY_FALLBACK_BG_WARM_MAX = max(
    0,
    int(os.getenv("LABELER_IDENTIFY_FALLBACK_BG_WARM_MAX", "12") or "12"),
)
_IDENTIFY_FALLBACK_MAX_MS = max(
    0.0,
    float(os.getenv("LABELER_IDENTIFY_FALLBACK_MAX_MS", "1200") or "1200"),
)
_IDENTIFY_FALLBACK_PREFETCH_MAX_MS = max(
    0.0,
    float(os.getenv("LABELER_IDENTIFY_FALLBACK_PREFETCH_MAX_MS", "300") or "300"),
)
_manual_sheet_ref_lock = asyncio.Lock()
_manual_sheet_ref_cache: Dict[str, List[Dict[str, Any]]] = {}
_manual_sheet_ref_built_mono: float = 0.0
_sheet_crop_index_lock = asyncio.Lock()
_sheet_crop_index_cache: Dict[Tuple[int, int], Dict[str, Any]] = {}
_sheet_crop_index_built_mono: float = 0.0
_ref_crop_result_cache: Dict[str, Tuple[float, bytes]] = {}
_ref_crop_negative_cache: Dict[Tuple[int, int], float] = {}
_REF_CROP_RESULT_CACHE_MAX = max(200, int(os.getenv("LABELER_REF_CROP_RESULT_CACHE_MAX", "3000") or "3000"))
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
_PROFILE_REFRESH_MIN_SEC = max(60, int(os.getenv("LABELER_PROFILE_REFRESH_MIN_SEC", "300") or "300"))
_profile_refresh_mono: float = 0.0
_UI_DIAG_VERBOSE = str(
    os.getenv("LABELER_UI_DIAG_VERBOSE", "0")
).strip().lower() in {"1", "true", "yes", "on"}
_QUEUE_CACHE_WARM_COOLDOWN_SEC = max(
    1.0,
    float(os.getenv("LABELER_QUEUE_CACHE_WARM_COOLDOWN_SEC", "8") or "8"),
)
_queue_cache_warm_next_mono: Dict[str, float] = {"detect": 0.0, "classify": 0.0, "manual": 0.0}


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
        # If a ref claims a concrete sheet serial/crop but that crop no longer exists in the
        # current sheet state, treat it as stale gallery metadata and hide it.
        if (
            serial_i is not None
            and crop_i is not None
            and serial_i > 0
            and crop_i > 0
            and _sheet_crop_index_cache
            and (int(serial_i), int(crop_i)) not in _sheet_crop_index_cache
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


def _maybe_schedule_queue_cache_warm(mode: str, queue: List[Dict[str, Any]]) -> None:
    """Throttle repeated queue cache warm kicks to reduce redundant disk/network churn."""
    if not queue:
        return
    key = str(mode or "").strip().lower()
    if key not in _queue_cache_warm_next_mono:
        key = "detect"
    now = time.monotonic()
    if now < float(_queue_cache_warm_next_mono.get(key, 0.0)):
        return
    _queue_cache_warm_next_mono[key] = now + float(_QUEUE_CACHE_WARM_COOLDOWN_SEC)
    warm_target = min(25, len(queue))
    try:
        asyncio.create_task(labeler_cache.ensure_cache_filled(queue[:60], target_count=warm_target))
    except Exception:
        pass


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


def _manual_sheet_ref_reservoir_add(
    refs: Dict[str, List[Dict[str, Any]]],
    counts: Dict[str, int],
    cat_key: str,
    entry: Dict[str, Any],
    *,
    limit: Optional[int] = None,
) -> None:
    if not cat_key:
        return
    lim = max(1, int(limit if limit is not None else _MANUAL_SHEET_REF_SAMPLE_PER_CAT))
    bucket = refs.setdefault(cat_key, [])
    seen_n = int(counts.get(cat_key, 0)) + 1
    counts[cat_key] = seen_n
    if len(bucket) < lim:
        bucket.append(entry)
        return
    j = random.randint(1, seen_n)
    if j <= lim:
        bucket[j - 1] = entry


def _build_manual_sheet_ref_cache(alias_lookup: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    rows = get_tcb_pics_rows(ttl_sec=60)
    refs: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}

    for row in rows[1:]:
        if len(row) <= COL_URL:
            continue
        url = str(row[COL_URL] if len(row) > COL_URL else "").strip()
        if not url.startswith("http"):
            continue
        serial = _parse_serial(str(row[COL_SERIAL] if len(row) > COL_SERIAL else ""))
        if serial is not None and _is_flagged_ref_serial(serial):
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
            _manual_sheet_ref_reservoir_add(
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
                limit=_MANUAL_SHEET_REF_CROPPED_SAMPLE_PER_CAT,
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
            _manual_sheet_ref_reservoir_add(
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
                limit=_MANUAL_SHEET_REF_CROPPED_SAMPLE_PER_CAT,
            )

        # Fallback: uncropped row image by CatID.
        for meta in catid_metas:
            _manual_sheet_ref_reservoir_add(
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
                limit=_MANUAL_SHEET_REF_UNCROPPED_SAMPLE_PER_CAT,
            )

    return refs


def _build_sheet_crop_index_cache() -> Dict[Tuple[int, int], Dict[str, Any]]:
    rows = get_tcb_pics_rows(ttl_sec=60)
    out: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for row in rows[1:]:
        if len(row) <= COL_URL:
            continue
        url = str(row[COL_URL] if len(row) > COL_URL else "").strip()
        if not url.startswith("http"):
            continue
        serial = _parse_serial(str(row[COL_SERIAL] if len(row) > COL_SERIAL else ""))
        if serial is None:
            continue
        if _is_flagged_ref_serial(serial):
            continue
        box_coords = str(row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
        if not box_coords or box_coords.lower() == "rejected":
            continue
        coords = [c.strip() for c in box_coords.split("|")]
        for i, coord in enumerate(coords):
            if not coord:
                continue
            if _parse_yolo_box_str(coord) is None:
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
            }
    return out


async def _ensure_sheet_crop_index_cache(force: bool = False) -> None:
    global _sheet_crop_index_cache, _sheet_crop_index_built_mono
    now = time.monotonic()
    if (
        not force
        and _sheet_crop_index_cache
        and (now - float(_sheet_crop_index_built_mono)) < _MANUAL_SHEET_REF_TTL_SEC
    ):
        return
    async with _sheet_crop_index_lock:
        now2 = time.monotonic()
        if (
            not force
            and _sheet_crop_index_cache
            and (now2 - float(_sheet_crop_index_built_mono)) < _MANUAL_SHEET_REF_TTL_SEC
        ):
            return
        _sheet_crop_index_cache = await asyncio.to_thread(_build_sheet_crop_index_cache)
        _sheet_crop_index_built_mono = time.monotonic()


def _sheet_ref_crop_url(serial: int, crop: int) -> str:
    return f"/api/labeler/ref_crop/{int(serial)}/{int(crop)}"


def _map_identify_candidate_refs_to_sheet(
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
        entry = _sheet_crop_index_cache.get(key) or {}
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
        is_cached = False
        try:
            is_cached = bool(labeler_cache.has_cached_image(int(serial)))
        except Exception:
            is_cached = False
        if is_cached:
            cached_first.append((serial, crop, box))
        else:
            uncached.append((serial, crop, box))

    ordered = cached_first + uncached
    for serial, crop, box in ordered:
        out.append({
            "img": "",
            "url": _sheet_ref_crop_url(serial, crop),
            "serial": serial,
            "crop": crop,
            "box": box,
            "source": "sheet_crop",
        })
        if len(out) >= limit:
            break
    return out


async def _ensure_manual_sheet_ref_cache(alias_lookup: Dict[str, Dict[str, Any]], force: bool = False) -> None:
    global _manual_sheet_ref_cache, _manual_sheet_ref_built_mono
    now = time.monotonic()
    if (
        not force
        and _manual_sheet_ref_cache
        and (now - float(_manual_sheet_ref_built_mono)) < _MANUAL_SHEET_REF_TTL_SEC
    ):
        return
    async with _manual_sheet_ref_lock:
        now2 = time.monotonic()
        if (
            not force
            and _manual_sheet_ref_cache
            and (now2 - float(_manual_sheet_ref_built_mono)) < _MANUAL_SHEET_REF_TTL_SEC
        ):
            return
        _manual_sheet_ref_cache = await asyncio.to_thread(_build_manual_sheet_ref_cache, alias_lookup)
        _manual_sheet_ref_built_mono = time.monotonic()


def _fallback_refs_for_cat(
    cat_key: str,
    limit: int = _MANUAL_FALLBACK_REFS_PER_CAT,
    *,
    include_uncropped: bool = True,
    prefer_cached: bool = False,
    prefer_serial: Optional[int] = None,
) -> List[Dict[str, Any]]:
    entries = list(_manual_sheet_ref_cache.get(str(cat_key or ""), []) or [])
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
            try:
                if sn is None:
                    return False
                return bool(labeler_cache.has_cached_image(int(sn)))
            except Exception:
                return False

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
                ref_url = _sheet_ref_crop_url(int(serial), int(crop_num))
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


def _thumb_b64_from_crop(crop: Image.Image, size: int = 128) -> Optional[str]:
    try:
        out = crop.copy()
        out.thumbnail((int(size), int(size)))
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


async def _materialize_fallback_refs(
    refs: List[Dict[str, Any]],
    *,
    thumb_size: int = 128,
    allow_remote_fetch: bool = False,
    remote_timeout_sec: float = 0.0,
) -> List[Dict[str, Any]]:
    if not refs:
        return []
    image_cache: Dict[Tuple[Optional[int], str], Optional[bytes]] = {}
    out: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(4)

    async def _fetch_data(serial: Optional[int], url: str) -> Optional[bytes]:
        key = (serial, url)
        if key in image_cache:
            return image_cache[key]
        if not allow_remote_fetch:
            data: Optional[bytes] = None
            if serial is not None:
                try:
                    data = labeler_cache.get_cached_image(int(serial))
                except Exception:
                    data = None
            image_cache[key] = data
            return data
        async with sem:
            if float(remote_timeout_sec or 0.0) > 0.0:
                try:
                    data = await asyncio.wait_for(
                        _fetch_image_bytes_for_labeler(serial, url),
                        timeout=float(remote_timeout_sec),
                    )
                except asyncio.TimeoutError:
                    data = None
            else:
                data = await _fetch_image_bytes_for_labeler(serial, url)
        image_cache[key] = data
        return data

    for ref in refs:
        row = dict(ref or {})
        box_str = str(row.get("box") or "").strip()
        box = _parse_yolo_box_str(box_str) if box_str else None
        serial_raw = row.get("serial")
        serial: Optional[int] = None
        try:
            if serial_raw is not None and str(serial_raw).strip():
                serial = int(serial_raw)
        except Exception:
            serial = None
        if serial is not None and _is_flagged_ref_serial(serial):
            continue
        url = str(row.get("url") or "").strip()
        fetch_url = url if url.startswith("http") else ""

        data = await _fetch_data(serial, fetch_url)
        if data:
            try:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                crop_img: Optional[Image.Image] = None
                if box is not None:
                    img_w, img_h = img.size
                    cx, cy, w, h = box
                    x1 = (cx - w / 2) * img_w
                    y1 = (cy - h / 2) * img_h
                    x2 = (cx + w / 2) * img_w
                    y2 = (cy + h / 2) * img_h
                    bw = x2 - x1
                    bh = y2 - y1
                    px = bw * float(settings.cv_pad_pct)
                    py = bh * float(settings.cv_pad_pct)
                    cx1 = max(0, int(round(x1 - px)))
                    cy1 = max(0, int(round(y1 - py)))
                    cx2 = min(int(img_w), int(round(x2 + px)))
                    cy2 = min(int(img_h), int(round(y2 + py)))
                    if cx2 > cx1 and cy2 > cy1:
                        crop_img = img.crop((cx1, cy1, cx2, cy2))
                else:
                    # For uncropped fallback refs, emit a thumbnail of the full frame.
                    crop_img = img

                if crop_img is not None:
                    b64 = _thumb_b64_from_crop(crop_img, size=thumb_size)
                    if b64:
                        row["img"] = b64
                        row["url"] = ""
            except Exception:
                pass

        # Keep legacy URL fallback for refs that could not be materialized.
        if not str(row.get("img") or "").strip():
            if serial is not None and not str(row.get("url") or "").strip():
                row["url"] = f"/api/labeler/cached_image/{int(serial)}"
        out.append(row)
    return out


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
    """Add CORS headers to response."""
    origin = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _boxes_to_yolo_strings(boxes: List[Tuple[float, float, float, float]]) -> List[str]:
    out: List[str] = []
    for box in boxes:
        try:
            cx, cy, w, h = [float(x) for x in box]
        except Exception:
            continue
        out.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return out


def _hash_cache_key(*parts: Any) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


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


def _cache_get_bytes(cache: Dict[str, Tuple[float, bytes]], key: str) -> Optional[bytes]:
    rec = cache.get(str(key))
    if not rec:
        return None
    ts, payload = rec
    if (time.monotonic() - float(ts)) > _CV_RESULT_TTL_SEC:
        cache.pop(str(key), None)
        return None
    return bytes(payload)


def _cache_set_bytes(
    cache: Dict[str, Tuple[float, bytes]],
    key: str,
    payload: bytes,
    *,
    max_items: int,
) -> None:
    now = time.monotonic()
    cache[str(key)] = (now, bytes(payload))

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
    """Best-effort image fetch: cache first, then cache downloader, then direct HTTP fallback."""
    data: Optional[bytes] = None
    serial_i = int(serial) if serial is not None else None

    if serial_i is not None:
        try:
            data = labeler_cache.get_cached_image(serial_i)
        except Exception:
            data = None
    if data:
        return data

    u = str(url or "").strip()
    if not u.startswith("http"):
        return None

    lower_u = u.lower()
    is_drive_like = (
        "drive.google.com" in lower_u
        or "drive.usercontent.google.com" in lower_u
        or "googleusercontent.com" in lower_u
    )

    if serial_i is not None:
        try:
            data = await labeler_cache.get_or_download(
                serial_i,
                u,
                bypass_backoff=bypass_backoff,
                max_attempts=(5 if bypass_backoff else 2),
            )
        except Exception:
            data = None
    if data:
        return data

    # labeler_cache already tries multiple Google Drive URL variants; avoid a second
    # long fallback request path that tends to duplicate timeout noise.
    if is_drive_like:
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(u) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                return data or None
    except Exception as e:
        log_action("labeler_image_fetch_error", f"serial={serial_i}", f"{type(e).__name__}: {e!r}")
        return None


def _kickoff_fallback_ref_cache_warm(candidates: List[Tuple[int, str]], *, max_items: int = 12) -> int:
    """Best-effort background warm of missing fallback ref source images."""
    uniq: List[Tuple[int, str]] = []
    seen: Set[int] = set()
    for serial, url in candidates:
        try:
            sn = int(serial)
        except Exception:
            continue
        u = str(url or "").strip()
        if sn <= 0 or not u.startswith("http"):
            continue
        if sn in seen:
            continue
        seen.add(sn)
        uniq.append((sn, u))
        if len(uniq) >= max(1, int(max_items)):
            break
    if not uniq:
        return 0

    async def _runner(rows: List[Tuple[int, str]]) -> None:
        sem = asyncio.Semaphore(4)

        async def _one(sn: int, u: str) -> None:
            async with sem:
                try:
                    await labeler_cache.get_or_download(int(sn), str(u))
                except Exception:
                    pass

        try:
            await asyncio.gather(*[_one(sn, u) for sn, u in rows])
        except Exception:
            pass

    try:
        asyncio.create_task(_runner(uniq))
    except Exception:
        return 0
    return len(uniq)


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


def _cache_get_classify_quality(serial: int) -> Optional[Tuple[bool, int, int, float]]:
    rec = _classify_quality_cache.get(int(serial))
    if not rec:
        return None
    ts, ok, w, h, blur = rec
    if (time.monotonic() - float(ts)) > _CLASSIFY_PREFILTER_CACHE_TTL_SEC:
        _classify_quality_cache.pop(int(serial), None)
        return None
    return bool(ok), int(w), int(h), float(blur)


def _cache_set_classify_quality(serial: int, ok: bool, width: int, height: int, blur: float) -> None:
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


async def _evaluate_classify_quality(serial: int, url: str) -> Tuple[bool, Dict[str, Any]]:
    """Evaluate image quality against classify queue gates."""
    if _CLASSIFY_MIN_PIXELS <= 0 and _CLASSIFY_MIN_DIM <= 0 and _CLASSIFY_MIN_BLUR <= 0:
        return True, {"width": 0, "height": 0, "pixels": 0, "blur": 0.0, "reasons": []}
    cached = _cache_get_classify_quality(int(serial))
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

    data = await _fetch_image_bytes_for_labeler(int(serial), str(url or "").strip())
    if not data:
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
        with Image.open(io.BytesIO(data)) as img:
            width, height = [int(x) for x in img.size]
            blur = _compute_blur_score(img)
    except Exception:
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


def _evaluate_cached_classify_quality(serial: int) -> Optional[Tuple[bool, Dict[str, Any]]]:
    cached = _cache_get_classify_quality(int(serial))
    if cached is None:
        return None
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


def _auto_reject_low_quality_sync(items: List[Dict[str, Any]]) -> int:
    """Mark low-quality classify rows as Rejected in BoxCatIDs."""
    if not items:
        return 0
    gc = sheets_client()
    sh = gc.open_by_key(settings.sheet_catabase_id)
    ws = sh.worksheet("TCB Pics Formatted")
    rows = ws.get_all_values()
    headers = rows[0] if rows else []
    col_labeled_by = _find_header_col(
        headers,
        [
            "LabeledBy",
            "LabelledBy",
            "Labeled By",
            "Labelled By",
            "labeled_by",
            "labelled_by",
        ],
    )
    serial_to_row: Dict[int, int] = {}
    serial_to_row_data: Dict[int, List[str]] = {}
    serial_to_labeled_by: Dict[int, str] = {}
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) <= COL_SERIAL:
            continue
        sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
        if sn is None:
            continue
        serial_to_row[int(sn)] = int(idx)
        serial_to_row_data[int(sn)] = row
        if col_labeled_by is not None:
            serial_to_labeled_by[int(sn)] = row[col_labeled_by] if len(row) > col_labeled_by else ""

    updates: List[Dict[str, Any]] = []
    applied = 0
    for item in items:
        try:
            sn = int(item.get("serial") or 0)
        except Exception:
            continue
        row_num = serial_to_row.get(sn)
        if not row_num:
            continue
        row = serial_to_row_data.get(sn, [])
        cur_box_coords = str(row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
        cur_box_cat_ids = str(row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "").strip()
        if not cur_box_coords or cur_box_coords.lower() == "rejected":
            continue
        cur_boxes = [c for c in cur_box_coords.split("|") if str(c).strip()]
        cur_labels = [c for c in cur_box_cat_ids.split("|") if str(c).strip()]
        if cur_boxes and len(cur_labels) >= len(cur_boxes):
            # Skip if this row is no longer waiting in classify queue.
            continue
        reject_labels = _build_rejected_labels(int(item.get("num_boxes") or 0))
        updates.append({
            "range": f"J{row_num}",
            "values": [[reject_labels]],
        })
        updates.append({
            "range": f"A{row_num}",
            "values": [[""]],
        })
        if col_labeled_by is not None:
            merged = _merge_labeled_by(serial_to_labeled_by.get(sn, ""), "auto-quality-filter")
            updates.append({
                "range": f"{_col_to_a1(col_labeled_by + 1)}{row_num}",
                "values": [[merged]],
            })
        applied += 1

    if not updates:
        return 0

    import time as _time
    chunk_size = 50
    for i in range(0, len(updates), chunk_size):
        ws.batch_update(updates[i:i + chunk_size])
        if i + chunk_size < len(updates):
            _time.sleep(1)
    try:
        force_refresh_tcb_cache()
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

async def get_queue_detect(request: web.Request) -> web.Response:
    """Return list of serials needing detector labels (empty BoxCoordinates)."""
    try:
        _kick_detector_warm_task()
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        rows = force_refresh_tcb_cache() if force else get_tcb_pics_rows(ttl_sec=60)
        user_id, _ = _actor_from_request(request)
        claims = await _claims_snapshot()
        queue = []
        for row in rows[1:]:  #Skip header
            if len(row) <= COL_SERIAL:
                continue
            sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if sn is None:
                continue
            claim = claims.get(("detect", int(sn)))
            if claim and str(claim.get("user_id") or "") != user_id:
                continue
            box_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
            if not box_coords.strip():
                url = row[COL_URL] if len(row) > COL_URL else ""
                if url.startswith("http"):
                    queue.append({"serial": sn, "url": url})
        queue.sort(key=lambda item: int(item.get("serial") or 0))
        total = len(queue)
        #Trigger background cache fill for first images in queue (throttled)
        _maybe_schedule_queue_cache_warm("detect", queue)
        return _with_cors(web.json_response({"queue": queue[:500], "total": total}), request)
    except Exception as e:
        log_action("labeler_queue_detect_error", "error", str(e))
        return _with_cors(web.Response(status=500, text="Internal server error"), request)


async def get_queue_classify(request: web.Request) -> web.Response:
    """Return serials with boxes but incomplete cat IDs."""
    try:
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        rows = force_refresh_tcb_cache() if force else get_tcb_pics_rows(ttl_sec=60)
        user_id, _ = _actor_from_request(request)
        claims = await _claims_snapshot()
        queue: List[Dict[str, Any]] = []
        candidates: List[Dict[str, Any]] = []
        skipped_low_quality = 0
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if sn is None:
                continue
            claim = claims.get(("classify", int(sn)))
            if claim and str(claim.get("user_id") or "") != user_id:
                continue
            box_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
            box_cat_ids = row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else ""
            
            #Skip if no boxes, rejected, or empty
            if not box_coords.strip() or box_coords.strip().lower() == "rejected":
                continue
            
            # Count only valid YOLO boxes to keep queue math aligned with identify parsing.
            parsed_boxes = [
                b
                for b in (str(box_coords).split("|") if box_coords else [])
                if _parse_yolo_box_str(str(b).strip()) is not None
            ]
            num_boxes = len(parsed_boxes)
            if num_boxes <= 0:
                continue
            labels = box_cat_ids.split("|") if box_cat_ids else []
            num_labeled = 0
            for idx in range(min(num_boxes, len(labels))):
                if str(labels[idx] or "").strip():
                    num_labeled += 1
            
            if num_labeled < num_boxes:
                url = row[COL_URL] if len(row) > COL_URL else ""
                if url.startswith("http"):
                    candidates.append({
                        "serial": sn,
                        "url": url,
                        "boxes": box_coords,
                        "labels": box_cat_ids,
                        "num_boxes": num_boxes,
                        "num_labeled": num_labeled,
                    })

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
                    "reason": ",".join((meta or {}).get("reasons") or []),
                    "pixels": int((meta or {}).get("pixels") or 0),
                    "blur": float((meta or {}).get("blur") or 0.0),
                })

        # Evaluate only a small subset synchronously to keep queue endpoint fast.
        sync_n = min(len(pending_items), int(_CLASSIFY_PREFILTER_MAX_SYNC_ITEMS))
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
            for item in deferred_items:
                row = dict(item)
                row["quality_pending"] = True
                queue.append(row)
            _schedule_classify_quality_scan(deferred_items)
        if auto_reject_items:
            _schedule_auto_reject_low_quality(auto_reject_items)

        queue.sort(key=lambda item: int(item.get("serial") or 0))
        total = len(queue)
        #Trigger background cache fill for first images in queue (throttled)
        _maybe_schedule_queue_cache_warm("classify", queue)
        payload = {
            "queue": queue[:500],
            "total": total,
            "filtered_low_quality": int(skipped_low_quality),
            "pending_quality_scan": int(len(deferred_items)),
            "classify_min_pixels": int(_CLASSIFY_MIN_PIXELS),
            "classify_min_dim": int(_CLASSIFY_MIN_DIM),
            "classify_min_blur": float(_CLASSIFY_MIN_BLUR),
        }
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_queue_classify_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_queue_manual(request: web.Request) -> web.Response:
    """Return serials with one or more crops marked NeedsReview."""
    try:
        force = str(request.query.get("force") or "").strip().lower() in {"1", "true", "yes", "y"}
        rows = force_refresh_tcb_cache() if force else get_tcb_pics_rows(ttl_sec=60)
        user_id, _ = _actor_from_request(request)
        claims = await _claims_snapshot()
        queue: List[Dict[str, Any]] = []
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if sn is None:
                continue
            claim = claims.get(("manual", int(sn)))
            if claim and str(claim.get("user_id") or "") != user_id:
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
            if not str(url).startswith("http"):
                continue
            queue.append({
                "serial": sn,
                "url": url,
                "boxes": box_coords,
                "labels": box_cat_ids,
                "num_boxes": len(coords),
                "review_indices": review_indices,
                "num_review": len(review_indices),
            })
        queue.sort(key=lambda item: int(item.get("serial") or 0))
        total = len(queue)
        _maybe_schedule_queue_cache_warm("manual", queue)
        return _with_cors(web.json_response({"queue": queue[:500], "total": total}), request)
    except Exception as e:
        log_action("labeler_queue_manual_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_image(request: web.Request) -> web.Response:
    """Get image data and annotations for a specific serial."""
    try:
        sn_str = request.match_info.get("sn", "")
        sn = _parse_serial(sn_str)
        if sn is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)
        
        rows = get_tcb_pics_rows(ttl_sec=60)
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if row_sn == sn:
                return _with_cors(web.json_response({
                    "serial": sn,
                    "url": row[COL_URL] if len(row) > COL_URL else "",
                    "cat_id": row[COL_CAT_ID] if len(row) > COL_CAT_ID else "",
                    "box_coords": row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "",
                    "box_cat_ids": row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "",
                }), request)
        
        return _with_cors(web.Response(status=404, text="Serial not found"), request)
    except Exception as e:
        log_action("labeler_get_image_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_cached_image(request: web.Request) -> web.Response:
    """Get cached image bytes for a serial. Downloads on-demand if not cached."""
    try:
        sn_str = request.match_info.get("sn", "")
        sn = _parse_serial(sn_str)
        if sn is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)
        
        #Try cache first
        data = labeler_cache.get_cached_image(sn)
        if data:
            resp = web.Response(body=data, content_type="image/jpeg")
            return _with_cors(resp, request)
        
        #Not cached - look up URL and download directly
        rows = get_tcb_pics_rows(ttl_sec=60)
        url = None
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if row_sn == sn:
                url = row[COL_URL] if len(row) > COL_URL else ""
                break
        
        if not url or not url.startswith("http"):
            return _with_cors(web.Response(status=404, text="Image URL not found"), request)

        # Do not block UI response on backend cache download attempts.
        # Serve source immediately and warm cache in background.
        async def _warm() -> None:
            try:
                await labeler_cache.get_or_download(
                    int(sn),
                    str(url),
                    bypass_backoff=True,
                    max_attempts=3,
                )
            except Exception:
                pass

        try:
            asyncio.create_task(_warm())
        except Exception:
            pass

        resp = web.Response(status=307)
        resp.headers["Location"] = str(url)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Labeler-Cache"] = "miss-redirect"
        return _with_cors(resp, request)
    except Exception as e:
        log_action("labeler_cached_image_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_ref_crop(request: web.Request) -> web.Response:
    """Return one sheet-defined crop as JPEG for classifier/manual reference cards."""
    try:
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
        cache_key = _hash_cache_key("ref_crop", int(sn), int(crop_num), int(thumb_size))
        cached = _cache_get_bytes(_ref_crop_result_cache, cache_key)
        if cached:
            return _with_cors(web.Response(body=cached, content_type="image/jpeg"), request)

        # Avoid spamming downstream storage/network and logs for known-bad refs.
        neg_key = (int(sn), int(crop_num))
        bad_until = _ref_crop_negative_cache.get(neg_key, 0.0)
        if bad_until and time.monotonic() < float(bad_until):
            return _with_cors(web.Response(status=404, text="Crop not found"), request)

        await _ensure_sheet_crop_index_cache(force=False)
        entry = _sheet_crop_index_cache.get((int(sn), int(crop_num)))
        if not entry:
            await _ensure_sheet_crop_index_cache(force=True)
            entry = _sheet_crop_index_cache.get((int(sn), int(crop_num)))
        if not entry:
            _log_ref_crop_miss(int(sn), int(crop_num), "sheet_entry_missing")
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 600.0
            return _with_cors(web.Response(status=404, text="Crop not found"), request)

        box = _parse_yolo_box_str(str(entry.get("box") or "").strip())
        if box is None:
            _log_ref_crop_miss(int(sn), int(crop_num), "box_missing")
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 600.0
            return _with_cors(web.Response(status=404, text="Crop coordinates missing"), request)

        image_bytes = labeler_cache.get_cached_image(int(sn))
        if not image_bytes:
            image_bytes = await _fetch_image_bytes_for_labeler(
                int(sn),
                str(entry.get("url") or ""),
                bypass_backoff=True,
            )
        if not image_bytes:
            _log_ref_crop_miss(int(sn), int(crop_num), "image_unavailable")
            # Treat source-image fetch failures as transient; avoid long false negatives.
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 20.0
            return _with_cors(web.Response(status=502, text="Source image unavailable"), request)

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_w, img_h = img.size
        cx, cy, w, h = box
        x1 = (cx - w / 2) * img_w
        y1 = (cy - h / 2) * img_h
        x2 = (cx + w / 2) * img_w
        y2 = (cy + h / 2) * img_h
        pad = float(settings.cv_pad_pct)
        bw = x2 - x1
        bh = y2 - y1
        px = bw * pad
        py = bh * pad
        cx1 = max(0, int(round(x1 - px)))
        cy1 = max(0, int(round(y1 - py)))
        cx2 = min(int(img_w), int(round(x2 + px)))
        cy2 = min(int(img_h), int(round(y2 + py)))
        if cx2 <= cx1 or cy2 <= cy1:
            _log_ref_crop_miss(
                int(sn),
                int(crop_num),
                "invalid_bounds",
                f"img={int(img_w)}x{int(img_h)}",
            )
            _ref_crop_negative_cache[neg_key] = time.monotonic() + 600.0
            return _with_cors(web.Response(status=422, text="Invalid crop bounds"), request)
        crop = img.crop((cx1, cy1, cx2, cy2))
        crop.thumbnail((int(thumb_size), int(thumb_size)))

        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=86)
        payload = out.getvalue()
        _cache_set_bytes(
            _ref_crop_result_cache,
            cache_key,
            payload,
            max_items=_REF_CROP_RESULT_CACHE_MAX,
        )
        return _with_cors(web.Response(body=payload, content_type="image/jpeg"), request)
    except Exception as e:
        log_action("labeler_ref_crop_error", "error", f"{type(e).__name__}: {e!r}")
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def post_ui_diag(request: web.Request) -> web.Response:
    """Temporary UI diagnostics endpoint for warm/progress investigations."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        if _UI_DIAG_VERBOSE:
            event = str(data.get("event") or "ui_diag").strip().lower()[:64] or "ui_diag"
            mode = str(data.get("mode") or "").strip().lower()[:24]
            serial = _parse_serial(str(data.get("serial") or ""))
            detail = str(data.get("detail") or data.get("details") or "").strip()
            if len(detail) > 800:
                detail = detail[:800]
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
    try:
        try:
            data = await request.json()
        except Exception:
            data = {}
        mode = str(data.get("mode") or "detect").strip().lower()
        if mode not in {"detect", "classify", "manual"}:
            return _with_cors(web.Response(status=400, text="Invalid mode"), request)

        serial = _parse_serial(str(data.get("serial") or ""))
        if serial is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)

        action = str(data.get("action") or "acquire").strip().lower()
        user_id, username = _actor_from_request(request)
        if not user_id:
            return _with_cors(web.Response(status=401, text="Missing session user"), request)

        if action == "release":
            released = await _release_claim(mode, int(serial), user_id)
            return _with_cors(web.json_response({
                "ok": True,
                "released": bool(released),
                "mode": mode,
                "serial": int(serial),
            }), request)

        granted, owner = await _acquire_claim(mode, int(serial), user_id, username)
        return _with_cors(web.json_response({
            "ok": True,
            "granted": bool(granted),
            "mode": mode,
            "serial": int(serial),
            "claimed_by": owner.get("username") if owner else None,
            "claim_ttl_sec": _LABELER_CLAIM_TTL_SEC,
        }), request)
    except Exception as e:
        log_action("labeler_claim_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


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
        
        cache_key = _hash_cache_key("detect", serial_i or "", str(url or "").strip(), int(bool(fast)))
        cached_payload = _cache_get(_detect_result_cache, cache_key)
        if cached_payload is not None:
            cached_boxes = [
                b for b in str(cached_payload.get("boxes_yolo") or "").split("|") if str(b).strip()
            ]
            log_action(
                "labeler_detect_cache_hit",
                f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; fast={fast}",
                f"boxes={len(cached_boxes)}",
            )
            return _with_cors(web.json_response(cached_payload), request)

        image_bytes = None
        t_image = time.perf_counter()
        
        #Try cache first if serial provided
        if serial_i is not None:
            image_bytes = labeler_cache.get_cached_image(int(serial_i))
            if image_bytes:
                image_source = "cache"

        #If serial provided but not cached and no URL, look up URL by serial
        if serial_i is not None and not image_bytes and not url:
            rows = get_tcb_pics_rows(ttl_sec=60)
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
        boxed_jpeg: bytes = b""
        sam_refined: bool = False
        try:
            t_sem = time.perf_counter()
            if prefetch:
                try:
                    await asyncio.wait_for(_detect_sem.acquire(), timeout=0.05)
                    acquired = True
                    sem_wait_ms = (time.perf_counter() - t_sem) * 1000.0
                except asyncio.TimeoutError:
                    sem_wait_ms = (time.perf_counter() - t_sem) * 1000.0
                    log_action(
                        "labeler_detect_prefetch_skipped",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}; fast={fast}",
                        f"reason=busy; sem_wait_ms={int(round(sem_wait_ms))}",
                    )
                    return _with_cors(web.Response(status=429, text="Busy"), request)
            else:
                await _detect_sem.acquire()
                acquired = True
                sem_wait_ms = (time.perf_counter() - t_sem) * 1000.0

            # Always run one YOLO pass first; avoid rerunning YOLO after a SAM timeout.
            detect_timeout = _DETECT_PREFETCH_TIMEOUT_SEC if prefetch else _DETECT_TIMEOUT_SEC
            t_detect = time.perf_counter()
            detect_result = await asyncio.wait_for(
                asyncio.to_thread(V.detect, image_bytes),
                timeout=detect_timeout,
            )
            detect_ms = (time.perf_counter() - t_detect) * 1000.0
            boxed_jpeg = detect_result.boxed_jpeg or b""
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

            # Foreground full-feature detect: try bounded inline SAM refine on the YOLO boxes.
            refined_boxes_abs = list(raw_boxes_abs)
            if (not fast) and (not prefetch) and raw_boxes_abs:
                from PIL import Image

                img = Image.open(io.BytesIO(image_bytes))
                iw, ih = img.size
                yolo_boxes: List[Tuple[float, float, float, float]] = []
                for (x1, y1, x2, y2) in raw_boxes_abs:
                    cx = (x1 + x2) / 2 / iw
                    cy = (y1 + y2) / 2 / ih
                    w = (x2 - x1) / iw
                    h = (y2 - y1) / ih
                    yolo_boxes.append((cx, cy, w, h))
                try:
                    sam_timeout = max(1.0, float(_DETECT_INLINE_SAM_TIMEOUT_SEC))
                    refined_boxes_abs = await asyncio.wait_for(
                        asyncio.to_thread(V.refine_boxes, image_bytes, yolo_boxes, passes=1),
                        timeout=sam_timeout,
                    )
                    sam_refined = True
                except Exception:
                    # Keep YOLO boxes if inline SAM refine fails or times out.
                    refined_boxes_abs = list(raw_boxes_abs)
            elif (not fast) and (not prefetch):
                # No boxes to refine still counts as full detect path.
                sam_refined = True
        finally:
            if acquired:
                _detect_sem.release()
        
        #Encode boxed image as base64
        import base64
        boxed_b64 = base64.b64encode(boxed_jpeg).decode("ascii") if boxed_jpeg else ""
        
        #Convert boxes to YOLO normalized format (cx, cy, w, h)
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        iw, ih = img.size
        
        yolo_boxes = []
        boxes_out = refined_boxes_abs if refined_boxes_abs else raw_boxes_abs
        for (x1, y1, x2, y2) in boxes_out:
            cx = (x1 + x2) / 2 / iw
            cy = (y1 + y2) / 2 / ih
            w = (x2 - x1) / iw
            h = (y2 - y1) / ih
            yolo_boxes.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        
        payload = {
            "boxed_image": boxed_b64,
            "boxes": yolo_boxes,
            "boxes_yolo": "|".join(yolo_boxes),
            "sam_refined": bool(sam_refined),
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
                f"out_boxes={len(yolo_boxes)}; sam_refined={int(bool(sam_refined))}"
            ),
        )
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_detect_error", "error", f"{type(e).__name__}: {e!r}")
        return _with_cors(web.Response(status=500, text=str(e)), request)


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

        boxes_sig = "|".join(str(b).strip() for b in boxes_raw)
        cache_key = _hash_cache_key(
            "refine",
            serial_i or "",
            str(url or "").strip(),
            int(passes),
            boxes_sig,
        )
        cached_payload = _cache_get(_refine_result_cache, cache_key)
        if cached_payload is not None:
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
            image_bytes = labeler_cache.get_cached_image(int(serial_i))

        if serial_i is not None and not image_bytes and not url:
            rows = get_tcb_pics_rows(ttl_sec=60)
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
                    "fallback": "passthrough_no_image",
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
        try:
            if prefetch:
                try:
                    await asyncio.wait_for(_refine_sem.acquire(), timeout=0.05)
                    acquired = True
                except asyncio.TimeoutError:
                    return _with_cors(web.Response(status=429, text="Busy"), request)
            else:
                await _refine_sem.acquire()
                acquired = True

            try:
                refined = await asyncio.wait_for(
                    asyncio.to_thread(V.refine_boxes, image_bytes, boxes, passes=passes),
                    timeout=(_REFINE_PREFETCH_TIMEOUT_SEC if prefetch else _REFINE_TIMEOUT_SEC),
                )
            except Exception:
                #Fallback to original boxes if SAM refine fails or times out
                from PIL import Image
                img = Image.open(io.BytesIO(image_bytes))
                iw, ih = img.size
                refined = []
                for (cx, cy, w, h) in boxes:
                    x1 = (cx - w / 2) * iw
                    y1 = (cy - h / 2) * ih
                    x2 = (cx + w / 2) * iw
                    y2 = (cy + h / 2) * ih
                    refined.append((x1, y1, x2, y2))
        finally:
            if acquired:
                _refine_sem.release()

        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        iw, ih = img.size
        yolo_boxes = []
        for (x1, y1, x2, y2) in refined:
            cx = (x1 + x2) / 2 / iw
            cy = (y1 + y2) / 2 / ih
            w = (x2 - x1) / iw
            h = (y2 - y1) / ih
            yolo_boxes.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

        payload = {
            "boxes": yolo_boxes,
            "boxes_yolo": "|".join(yolo_boxes),
        }
        _cache_set(
            _refine_result_cache,
            cache_key,
            payload,
            max_items=_REFINE_RESULT_CACHE_MAX,
        )
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_refine_error", "error", f"{type(e).__name__}: {e!r}")
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def post_identify(request: web.Request) -> web.Response:
    """Run DINOv3 identification on crops from an image."""
    try:
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
        refs_per_full = max(1, int(getattr(settings, "labeler_ref_per_candidate", 5) or 5))
        refs_per_prefetch_default = refs_per_full
        refs_per = max(
            1,
            int(getattr(settings, "labeler_ref_per_candidate_prefetch", refs_per_prefetch_default) or refs_per_prefetch_default),
        ) if prefetch else refs_per_full
        refs_per_target = int(refs_per)
        # Ask vision for a deeper pool, then keep only the first N valid refs.
        # This avoids dropping below 5 when some high-rank refs are invalid/missing.
        refs_per_query = max(refs_per_target, refs_per_target * 3) if (not prefetch) else refs_per_target
        top_k = max(1, int(getattr(settings, "labeler_top_k", 9) or 9))

        boxes_sig = "|".join(str(b).strip() for b in boxes_raw)
        req_id = _hash_cache_key(
            "identify_req",
            time.time_ns(),
            serial_i or "",
            str(url or "").strip(),
            int(bool(prefetch)),
            focus_crop_idx if (not prefetch and focus_crop_idx is not None) else "",
            boxes_sig,
        )[:10]
        trace_identify = _identify_should_trace(prefetch)
        req_t0 = time.perf_counter()
        sem_wait_ms = 0.0
        identify_ms = 0.0
        image_fetch_ms = 0.0
        image_source = "none"
        enrich_ms = 0.0
        sheet_ref_ms = 0.0
        sheet_ref_applied = 0
        sheet_ref_total = 0
        sheet_ref_gallery = 0
        sheet_ref_fallback = 0
        singleflight_wait_ms = 0.0
        payload_for_singleflight: Optional[Dict[str, Any]] = None

        cache_key = _hash_cache_key(
            "identify",
            _IDENTIFY_REF_PIPELINE_VERSION,
            serial_i or "",
            str(url or "").strip(),
            int(bool(rerank)),
            int(top_k),
            int(refs_per_target),
            focus_crop_idx if (not prefetch and focus_crop_idx is not None) else "",
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
                        f"boxes={len(boxes_raw)}; top_k={top_k}; refs_per={refs_per_target}; "
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
            wait_timeout = min(3.0, _IDENTIFY_PREFETCH_TIMEOUT_SEC) if prefetch else max(75.0, _IDENTIFY_TIMEOUT_SEC + 30.0)
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
                    image_bytes = labeler_cache.get_cached_image(int(serial_i))
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
                        f"top_k={top_k}; refs_per={refs_per_target}; rerank={int(bool(rerank))}; "
                        f"focus={focus_crop_idx if focus_crop_idx is not None else -1}; "
                        f"img_src={image_source}; img_ms={int(round(image_fetch_ms))}; "
                        f"sf_wait_ms={int(round(singleflight_wait_ms))}"
                    ),
                )

            # Run identify on provided boxes (normalized cx,cy,w,h).
            # Prefetch requests should never monopolize worker capacity.
            acquired = False
            try:
                t_sem_wait = time.perf_counter()
                if prefetch:
                    try:
                        await asyncio.wait_for(_identify_sem.acquire(), timeout=0.05)
                        acquired = True
                        sem_wait_ms = (time.perf_counter() - t_sem_wait) * 1000.0
                    except asyncio.TimeoutError:
                        sem_wait_ms = (time.perf_counter() - t_sem_wait) * 1000.0
                        if trace_identify:
                            log_action(
                                "labeler_identify_prefetch_skipped",
                                f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                                f"reason=busy; sem_wait_ms={int(round(sem_wait_ms))}",
                            )
                        return _with_cors(web.Response(status=429, text="Busy"), request)
                else:
                    await _identify_sem.acquire()
                    acquired = True
                    sem_wait_ms = (time.perf_counter() - t_sem_wait) * 1000.0

                timeout_sec = (min(2.5, _IDENTIFY_PREFETCH_TIMEOUT_SEC) if prefetch else _IDENTIFY_TIMEOUT_SEC)
                t_identify = time.perf_counter()
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        V.identify_boxes,
                        image_bytes,
                        boxes,
                        rerank=rerank,
                        top_k=top_k,
                        refs_per=refs_per_query,
                        # Foreground classify requests should return inline thumbs so
                        # refs paint immediately without ref_crop network churn.
                        include_ref_thumbs=(not prefetch),
                    ),
                    timeout=timeout_sec,
                )
                identify_ms = (time.perf_counter() - t_identify) * 1000.0
            except asyncio.TimeoutError:
                log_action("labeler_identify_timeout", f"serial={serial}", f"prefetch={prefetch}")
                if trace_identify:
                    log_action(
                        "labeler_identify_timeout_trace",
                        f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                        (
                            f"sem_wait_ms={int(round(sem_wait_ms))}; "
                            f"timeout_s={timeout_sec}; img_ms={int(round(image_fetch_ms))}"
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
            # to ref-crop endpoints; no deterministic sheet fallback override.
            try:
                t_sheet = time.perf_counter()
                await _ensure_sheet_crop_index_cache(force=False)

                warm_candidates: List[Tuple[int, str]] = []
                for crop in [row for row in result.results if isinstance(row, dict)]:
                    for cand in crop.get("candidates", []) or []:
                        refs_raw = cand.get("refs")
                        ref_target = max(1, int(refs_per_target))
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
                                if len(selected_refs) >= ref_target:
                                    break
                                continue
                            if _is_flagged_ref_serial(int(serial_ref)):
                                continue
                            key_sc = (serial_ref, crop_ref)
                            if key_sc in seen_sc:
                                continue
                            entry = _sheet_crop_index_cache.get((int(serial_ref), int(crop_ref))) or {}
                            if not entry:
                                # Sheet no longer has this crop (often because it was cleared/flagged).
                                # Drop stale gallery refs instead of showing unlabeled historical thumbnails.
                                continue
                            seen_sc.add(key_sc)
                            ref_url = _sheet_ref_crop_url(serial_ref, crop_ref)
                            selected_refs.append({
                                "img": img_b64,
                                "url": ref_url,
                                "serial": serial_ref,
                                "crop": crop_ref,
                                "source": "dino_gallery",
                            })
                            if img_b64:
                                seen_img.add(img_b64)
                            src_url = str(entry.get("url") or "").strip()
                            if (not img_b64) and src_url.startswith("http"):
                                warm_candidates.append((int(serial_ref), src_url))
                            if len(selected_refs) >= ref_target:
                                break

                        cand["refs"] = selected_refs
                        if cand["refs"]:
                            sheet_ref_applied += 1
                        sheet_ref_total += len(cand["refs"])
                        sheet_ref_gallery += int(len(cand["refs"]))

                if warm_candidates:
                    _kickoff_fallback_ref_cache_warm(
                        warm_candidates,
                        max_items=max(12, min(len(warm_candidates), int(refs_per_target) * int(top_k))),
                    )

                sheet_ref_ms = (time.perf_counter() - t_sheet) * 1000.0
            except Exception as e:
                log_action(
                    "labeler_identify_sheet_refs_error",
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
            if trace_identify:
                stats = _identify_result_ref_stats(list(result.results or []))
                total_ms = (time.perf_counter() - req_t0) * 1000.0
                log_action(
                    "labeler_identify_done",
                    f"rid={req_id}; serial={serial_i}; prefetch={prefetch}",
                    (
                        f"ms total={int(round(total_ms))} img={int(round(image_fetch_ms))} "
                        f"sem={int(round(sem_wait_ms))} id={int(round(identify_ms))} "
                        f"enrich={int(round(enrich_ms))} sheet={int(round(sheet_ref_ms))}; "
                        f"src={image_source}; crops={stats['crops']}; cands={stats['cands']}; "
                        f"with_refs={stats['with_refs']}; zero_refs={stats['zero_refs']}; "
                        f"avg_refs={stats['avg_refs']}; max_refs={stats['max_refs']}; "
                        f"inline_refs={stats['inline_refs']}; url_refs={stats['url_refs']}; "
                        f"inline_cands={stats['inline_cands']}; url_only_cands={stats['url_only_cands']}; "
                        f"sheet_applied={int(sheet_ref_applied)}; sheet_total={int(sheet_ref_total)}; "
                        f"sheet_gallery={int(sheet_ref_gallery)}; sheet_fallback={int(sheet_ref_fallback)}; "
                        f"sf_wait={int(round(singleflight_wait_ms))}"
                    ),
                )
            return _with_cors(web.json_response(payload), request)
        finally:
            if singleflight_owner:
                await _identify_singleflight_finish(cache_key, singleflight_future, payload_for_singleflight)
    except Exception as e:
        log_action("labeler_identify_error", "error", f"{type(e).__name__}: {e!r}")
        return _with_cors(web.Response(status=500, text=str(e)), request)


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
            serial_i or "",
            str(url or "").strip(),
            box_raw,
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
                image_bytes = labeler_cache.get_cached_image(int(serial_i))
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
                    refs_per=_MANUAL_FALLBACK_REFS_PER_CAT,
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
            await _ensure_manual_sheet_ref_cache(alias_lookup, force=False)
            await _ensure_sheet_crop_index_cache(force=False)
        except Exception as e:
            # Keep manual mode usable even if sheet-ref sampling fails.
            log_action(
                "labeler_manual_sheet_ref_cache_error",
                "error",
                f"{type(e).__name__}: {e!r}",
            )

        by_key: Dict[str, Dict[str, Any]] = {}
        for cand in _enrich_manual_candidates(raw_candidates or [], alias_lookup):
            row = dict(cand)
            pkey = str(row.pop("profile_key", "")).strip()
            if not pkey:
                pkey = f"gallery::{_norm_cat_lookup_token(str(row.get('name') or ''))}"
            if pkey in by_key:
                continue
            # Gallery-backed cats should still have fallback refs if no local thumb was produced.
            if not row.get("refs"):
                row["refs"] = _fallback_refs_for_cat(
                    pkey,
                    limit=_MANUAL_FALLBACK_REFS_PER_CAT,
                    prefer_cached=True,
                )
            by_key[pkey] = row

        # Ensure every CatDatabase cat appears, even if not in current gallery embeddings.
        for meta in ordered_profile:
            key = str(meta.get("key") or "")
            if not key:
                continue
            existing = by_key.get(key)
            if existing:
                if not existing.get("refs"):
                    existing["refs"] = _fallback_refs_for_cat(
                        key,
                        limit=_MANUAL_FALLBACK_REFS_PER_CAT,
                        prefer_cached=True,
                    )
                if not existing.get("display_name"):
                    existing["display_name"] = str(meta.get("display_name") or existing.get("name") or "")
                if existing.get("cat_id") is None:
                    existing["cat_id"] = meta.get("cat_id")
                if not existing.get("desc") and meta.get("desc"):
                    existing["desc"] = str(meta.get("desc"))
                continue

            by_key[key] = {
                "name": str(meta.get("name") or ""),
                "display_name": str(meta.get("display_name") or meta.get("name") or ""),
                "cat_id": meta.get("cat_id"),
                "desc": str(meta.get("desc") or ""),
                "conf": None,  # Not present in gallery embeddings yet
                "refs": _fallback_refs_for_cat(
                    key,
                    limit=_MANUAL_FALLBACK_REFS_PER_CAT,
                    prefer_cached=True,
                ),
            }

        def _safe_cat_id(val: Any) -> int:
            try:
                if val is None or str(val).strip() == "":
                    return 10**9
                return int(val)
            except Exception:
                return 10**9

        def _candidate_sort_key(row: Dict[str, Any]) -> Tuple[int, str]:
            return (
                _safe_cat_id(row.get("cat_id")),
                str(row.get("display_name") or row.get("name") or ""),
            )

        candidates = sorted(list(by_key.values()), key=_candidate_sort_key)
        status = dict(V.labeler_manual_ref_status() or {})
        total_known = len(ordered_profile) if ordered_profile else len(candidates)
        status["total"] = max(int(status.get("total") or 0), int(total_known))
        status["cats"] = int(total_known)
        if status.get("ready"):
            status["built"] = int(status.get("total") or total_known)
        else:
            status["built"] = max(int(status.get("built") or 0), 0)

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
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        log_action("labeler_manual_candidates_error", "error", f"{type(e).__name__}: {e!r}")
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- Save Endpoint ----------

async def post_save(request: web.Request) -> web.Response:
    """Batch save annotations to the sheet."""
    try:
        global _manual_sheet_ref_cache, _manual_sheet_ref_built_mono
        global _sheet_crop_index_cache, _sheet_crop_index_built_mono
        data = await request.json()
        updates = data.get("updates", [])  #List of {serial, box_coords, box_cat_ids}
        _, actor_name = _actor_from_request(request)
        
        if not updates:
            return _with_cors(web.Response(status=400, text="No updates"), request)
        
        #Get sheet
        gc = sheets_client()
        sh = gc.open_by_key(settings.sheet_catabase_id)
        ws = sh.worksheet("TCB Pics Formatted")
        
        #Build serial -> row index mapping
        rows = ws.get_all_values()
        headers = rows[0] if rows else []
        col_labeled_by = _find_header_col(
            headers,
            [
                "LabeledBy",
                "LabelledBy",
                "Labeled By",
                "Labelled By",
                "labeled_by",
                "labelled_by",
            ],
        )
        needs_catid_sync = any("box_cat_ids" in upd for upd in updates)
        catid_lookup: Dict[str, str] = {}
        if needs_catid_sync:
            try:
                cat_ws = sh.worksheet("CatDatabase")
                cat_rows = cat_ws.get_all_values()
                catid_lookup = _build_catid_lookup(cat_rows)
            except Exception as e:
                log_action("labeler_catid_lookup_error", "error", f"{type(e).__name__}: {e!r}")
        can_sync_catid = bool(catid_lookup)

        serial_to_row = {}
        serial_to_labeled_by: Dict[int, str] = {}
        for idx, row in enumerate(rows[1:], start=2):  #1-indexed, skip header
            if len(row) > COL_SERIAL:
                sn = _parse_serial(row[COL_SERIAL])
                if sn is not None:
                    serial_to_row[sn] = idx
                    if col_labeled_by is not None:
                        serial_to_labeled_by[sn] = row[col_labeled_by] if len(row) > col_labeled_by else ""
        
        #Build cell updates
        import time as _time
        cells_to_update = []
        labeled_by_serials: set[int] = set()
        pending_unblacklist_ref_serials: List[int] = []
        cleared_ref_blacklist_serials: List[int] = []
        for upd in updates:
            sn = _parse_serial(str(upd.get("serial") or ""))
            if sn not in serial_to_row:
                continue
            row_num = serial_to_row[sn]
            touched = False
            if "box_coords" in upd:
                cells_to_update.append({
                    "range": f"I{row_num}",
                    "values": [[upd["box_coords"]]]
                })
                touched = True
            if "box_cat_ids" in upd:
                cells_to_update.append({
                    "range": f"J{row_num}",
                    "values": [[upd["box_cat_ids"]]]
                })
                if can_sync_catid:
                    catid_cell = _format_catid_cell_from_labels(str(upd.get("box_cat_ids") or ""), catid_lookup)
                    cells_to_update.append({
                        "range": f"A{row_num}",
                        "values": [[catid_cell]],
                    })
                if _box_cat_ids_has_reviewed_label(upd.get("box_cat_ids")):
                    pending_unblacklist_ref_serials.append(int(sn))
                touched = True
            if (
                touched
                and col_labeled_by is not None
                and sn not in labeled_by_serials
            ):
                merged = _merge_labeled_by(serial_to_labeled_by.get(sn, ""), actor_name)
                serial_to_labeled_by[sn] = merged
                labeled_by_serials.add(sn)
                cells_to_update.append({
                    "range": f"{_col_to_a1(col_labeled_by + 1)}{row_num}",
                    "values": [[merged]],
                })
        
        #Batch update with throttling
        chunk_size = 50
        for i in range(0, len(cells_to_update), chunk_size):
            chunk = cells_to_update[i:i + chunk_size]
            ws.batch_update(chunk)
            if i + chunk_size < len(cells_to_update):
                _time.sleep(1)

        for sn in pending_unblacklist_ref_serials:
            if _discard_flagged_ref_serial(int(sn)):
                cleared_ref_blacklist_serials.append(int(sn))

        #Refresh local sheet cache so queues reflect updates quickly.
        try:
            force_refresh_tcb_cache()
        except Exception:
            pass
        _manual_sheet_ref_cache = {}
        _manual_sheet_ref_built_mono = 0.0
        _sheet_crop_index_cache = {}
        _sheet_crop_index_built_mono = 0.0
        _ref_crop_result_cache.clear()
        
        log_action("labeler_save", "saved", f"{len(updates)} annotations by {actor_name}")
        return _with_cors(web.json_response({
            "status": "ok",
            "saved": len(updates),
            "unblacklisted_ref_serials": sorted(list(dict.fromkeys(cleared_ref_blacklist_serials))),
        }), request)
    except Exception as e:
        log_action("labeler_save_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def post_flag_incorrect(request: web.Request) -> web.Response:
    """Clear CatID + box labels for one serial so it can be relabeled."""
    global _manual_sheet_ref_cache, _manual_sheet_ref_built_mono
    global _sheet_crop_index_cache, _sheet_crop_index_built_mono
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
        gc = sheets_client()
        sh = gc.open_by_key(settings.sheet_catabase_id)
        ws = sh.worksheet("TCB Pics Formatted")
        rows = ws.get_all_values()
        headers = rows[0] if rows else []
        col_labeled_by = _find_header_col(
            headers,
            [
                "LabeledBy",
                "LabelledBy",
                "Labeled By",
                "Labelled By",
                "labeled_by",
                "labelled_by",
            ],
        )

        serial_to_row: Dict[int, int] = {}
        serial_to_row_data: Dict[int, List[str]] = {}
        serial_to_labeled_by: Dict[int, str] = {}
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) <= COL_SERIAL:
                continue
            sn = _parse_serial(str(row[COL_SERIAL] if len(row) > COL_SERIAL else ""))
            if sn is None:
                continue
            serial_to_row[int(sn)] = int(idx)
            serial_to_row_data[int(sn)] = row
            if col_labeled_by is not None:
                serial_to_labeled_by[int(sn)] = row[col_labeled_by] if len(row) > col_labeled_by else ""

        row_num = serial_to_row.get(int(serial))
        if not row_num:
            return _with_cors(web.Response(status=404, text="Serial not found"), request)

        row_data = serial_to_row_data.get(int(serial), [])
        prev_cat_id = str(row_data[COL_CAT_ID] if len(row_data) > COL_CAT_ID else "")
        prev_box_coords = str(row_data[COL_BOX_COORDS] if len(row_data) > COL_BOX_COORDS else "")
        prev_box_cat_ids = str(row_data[COL_BOX_CAT_IDS] if len(row_data) > COL_BOX_CAT_IDS else "")

        updates: List[Dict[str, Any]] = []
        changed = bool(prev_cat_id.strip() or prev_box_coords.strip() or prev_box_cat_ids.strip())
        if changed:
            updates.extend(
                [
                    {"range": f"A{row_num}", "values": [[""]]},
                    {"range": f"I{row_num}", "values": [[""]]},
                    {"range": f"J{row_num}", "values": [[""]]},
                ]
            )

        if col_labeled_by is not None:
            merged = _merge_labeled_by(serial_to_labeled_by.get(int(serial), ""), actor_name)
            if merged != serial_to_labeled_by.get(int(serial), ""):
                updates.append(
                    {
                        "range": f"{_col_to_a1(col_labeled_by + 1)}{row_num}",
                        "values": [[merged]],
                    }
                )

        if updates:
            ws.batch_update(updates)

        try:
            force_refresh_tcb_cache()
        except Exception:
            pass

        # Invalidate in-memory quality/candidate caches after label clears.
        _classify_quality_cache.pop(int(serial), None)
        _auto_reject_quality_inflight.discard(int(serial))
        _classify_quality_scan_inflight.discard(int(serial))
        _detect_result_cache.clear()
        _refine_result_cache.clear()
        _identify_result_cache.clear()
        _manual_result_cache.clear()
        _manual_sheet_ref_cache = {}
        _manual_sheet_ref_built_mono = 0.0
        _sheet_crop_index_cache = {}
        _sheet_crop_index_built_mono = 0.0
        _ref_crop_result_cache.clear()
        _add_flagged_ref_serial(int(serial))

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
            f"sn={int(serial)}; changed={1 if changed else 0}",
            f"by={actor_name}; {src_txt}",
        )

        return _with_cors(
            web.json_response(
                {
                    "status": "ok",
                    "serial": int(serial),
                    "changed": bool(changed),
                    "already_unlabeled": not bool(changed),
                    "blacklisted_for_refs": True,
                }
            ),
            request,
        )
    except Exception as e:
        log_action("labeler_flag_incorrect_error", "error", f"{type(e).__name__}: {e!r}")
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- Cat List Endpoint ----------

async def get_cats(request: web.Request) -> web.Response:
    """Return list of all known cat names from the gallery."""
    try:
        cats = await asyncio.to_thread(V.get_all_cats)
        return _with_cors(web.json_response({"cats": cats}), request)
    except Exception as e:
        log_action("labeler_get_cats_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


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
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("labeler_refs_warm_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_refs_status(request: web.Request) -> web.Response:
    """Get reference cache status."""
    try:
        return _with_cors(web.json_response(V.labeler_ref_status()), request)
    except Exception as e:
        log_action("labeler_refs_status_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def post_manual_refs_warm(request: web.Request) -> web.Response:
    """Warm the lighter manual-review reference cache."""
    try:
        force = False
        try:
            body = await request.json()
            force = bool(body.get("force"))
        except Exception:
            force = False
        status = await V.warm_labeler_manual_refs(force=force)
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("labeler_manual_refs_warm_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_manual_refs_status(request: web.Request) -> web.Response:
    """Get manual-review reference cache status."""
    try:
        return _with_cors(web.json_response(V.labeler_manual_ref_status()), request)
    except Exception as e:
        log_action("labeler_manual_refs_status_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_gallery_retrain_status_api(request: web.Request) -> web.Response:
    """Read current 4AM gallery retrain schedule/status."""
    try:
        status = await get_gallery_retrain_status()
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("gallery_retrain_status_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def post_gallery_retrain_schedule_api(request: web.Request) -> web.Response:
    """Schedule the next 4AM full gallery retrain run."""
    try:
        user_id, actor_name = _actor_from_request(request)
        requester = actor_name if actor_name else (user_id or "unknown")
        status = await schedule_gallery_retrain(requested_by=requester)
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("gallery_retrain_schedule_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- OPTIONS handlers for CORS ----------

async def options_handler(request: web.Request) -> web.Response:
    """Handle CORS preflight requests."""
    resp = web.Response(status=204)
    return _with_cors(resp, request)


#---------- Route registration ----------

def get_labeler_routes() -> List:
    """Return list of labeler API routes for registration in main.py."""
    return [
        web.get("/api/labeler/queue/detect", get_queue_detect),
        web.get("/api/labeler/queue/classify", get_queue_classify),
        web.get("/api/labeler/queue/manual", get_queue_manual),
        web.post("/api/labeler/claim", post_claim),
        web.get("/api/labeler/image/{sn}", get_image),
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
        web.get("/api/labeler/gallery_retrain/status", get_gallery_retrain_status_api),
        web.post("/api/labeler/gallery_retrain/schedule", post_gallery_retrain_schedule_api),
        #CORS preflight
        web.options("/api/labeler/queue/detect", options_handler),
        web.options("/api/labeler/queue/classify", options_handler),
        web.options("/api/labeler/queue/manual", options_handler),
        web.options("/api/labeler/claim", options_handler),
        web.options("/api/labeler/image/{sn}", options_handler),
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
        web.options("/api/labeler/gallery_retrain/status", options_handler),
        web.options("/api/labeler/gallery_retrain/schedule", options_handler),
    ]
