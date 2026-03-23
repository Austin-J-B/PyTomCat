"""Utilities for running YOLO detection and DINOv3 ReID similarity for TomCat."""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import os
import math
import warnings
import threading
import time
import base64
import asyncio
import random
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any, Callable, cast

from PIL import Image, ImageDraw, ImageFont, ImageOps
import torch
from torch import Tensor

#Keep Ultralytics config within the repo
os.environ.setdefault(
    "YOLO_CONFIG_DIR",
    str(Path(__file__).resolve().parents[2] / ".ultra"),
)

warnings.filterwarnings("ignore", message=".*Torch was not compiled with flash attention.*")

try:
    from ultralytics import YOLO, SAM
except Exception:
    YOLO = None
    SAM = None

from ..config import settings, _find_latest_local_gallery
from ..logger import log_action
from ..services.station_residents import get_active_cat_station_membership

#---------- Constants ----------
_PURPLE = "#4C007F"
_DEFAULT_CONF = 0.552
_LABELER_SAM_PROMPT_PAD_PCT = max(
    0.0,
    float(os.getenv("LABELER_SAM_PROMPT_PAD_PCT", "0.03") or "0.03"),
)
_LABELER_SAM_GUARD_PAD_PCT = max(
    _LABELER_SAM_PROMPT_PAD_PCT,
    float(os.getenv("LABELER_SAM_GUARD_PAD_PCT", "0.08") or "0.08"),
)
_LABELER_SAM_MAX_OUTSIDE_GUARD_RATIO = min(
    1.0,
    max(
        0.0,
        float(os.getenv("LABELER_SAM_MAX_OUTSIDE_GUARD_RATIO", "0.08") or "0.08"),
    ),
)
_LABELER_SAM_MIN_DETECTOR_MASK_RATIO = min(
    1.0,
    max(
        0.0,
        float(os.getenv("LABELER_SAM_MIN_DETECTOR_MASK_RATIO", "0.35") or "0.35"),
    ),
)
_LABELER_SAM_MIN_DETECTOR_COVERAGE = min(
    1.0,
    max(
        0.0,
        float(os.getenv("LABELER_SAM_MIN_DETECTOR_COVERAGE", "0.30") or "0.30"),
    ),
)
_LABELER_SAM_MAX_REFINED_AREA_RATIO = max(
    1.0,
    float(os.getenv("LABELER_SAM_MAX_REFINED_AREA_RATIO", "1.09") or "1.09"),
)
_LABELER_SAM_TIGHT_OVERLAP_RATIO = min(
    1.0,
    max(
        0.0,
        float(os.getenv("LABELER_SAM_TIGHT_OVERLAP_RATIO", "0.62") or "0.62"),
    ),
)
_LABELER_SAM_MAX_EDGE_SHIFT_RATIO = min(
    1.0,
    max(
        0.0,
        float(os.getenv("LABELER_SAM_MAX_EDGE_SHIFT_RATIO", "0.06") or "0.06"),
    ),
)

#---------- Internal State ----------
_yolo: Optional[Any] = None
_sam: Optional[Any] = None
_sam_lock = threading.Lock()
_sam_failed: bool = False
_clf: Optional[torch.nn.Module] = None
_gallery_emb: Optional[Tensor] = None
_gallery_names: List[str] = []
_gallery_paths: List[str] = []
_gallery_records: List[dict[str, Any]] = []
_gallery_cat_indices: dict[str, Tensor] = {}
_gallery_root_hints: Optional[List[Path]] = None
_device: Optional[torch.device] = None
_half: bool = False
_font: Optional[Any] = None
_labeler_ref_cache: dict[str, dict[str, Any]] = {}
_labeler_ref_ready: bool = False
_labeler_ref_building: bool = False
_labeler_ref_task: Optional[asyncio.Task] = None
_labeler_ref_progress_total: int = 0
_labeler_ref_progress_built: int = 0
_manual_ref_cache: dict[str, dict[str, Any]] = {}
_manual_ref_ready: bool = False
_manual_ref_building: bool = False
_manual_ref_task: Optional[asyncio.Task] = None
_manual_ref_progress_total: int = 0
_manual_ref_progress_built: int = 0
_manual_ref_per_cat: int = 0
_thumb_cache: dict[tuple[str, int], str] = {}
_thumb_cache_max: int = max(200, int(os.getenv("LABELER_THUMB_CACHE_MAX", "2000") or "2000"))
_resolved_gallery_path_cache: dict[str, str] = {}
_gallery_crop_roots: Optional[List[Path]] = None

# Local photo metadata columns (0-based).
COL_SERIAL = 7
COL_BOX_COORDS = 8
COL_BOX_CAT_IDS = 9
SN_PATTERN = re.compile(r"sn(\d+)", re.IGNORECASE)
_CAT_ID_NAME_RE = re.compile(r"^\s*(\d+)\s*[.)\-:]?\s*(.+?)\s*$")
_CROP_NUM_PATTERN = re.compile(
    r"(?:crop[_\-\s]?(\d+))|(?:^|[_\-])c(\d{1,3})(?:[^0-9]|$)",
    re.IGNORECASE,
)
_RERANK_ENABLED = str(os.getenv("LABELER_RERANK_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
_RERANK_TOP_N = max(1, int(os.getenv("LABELER_RERANK_TOP_N", "15") or "15"))
_RERANK_HFLIP = str(os.getenv("LABELER_RERANK_HFLIP", "0")).strip().lower() in {"1", "true", "yes", "on"}
_LABELER_REF_SEARCH_POOL = max(5, int(os.getenv("LABELER_REF_SEARCH_POOL", "250") or "250"))
_DEFAULT_LABELER_REF_BUILD_WORKERS = max(4, min(32, int(os.cpu_count() or 8)))
_LABELER_REF_BUILD_WORKERS = max(
    1,
    int(os.getenv("LABELER_REF_BUILD_WORKERS", str(_DEFAULT_LABELER_REF_BUILD_WORKERS)) or str(_DEFAULT_LABELER_REF_BUILD_WORKERS)),
)
_IDENTIFY_STATION_PRIOR_ENABLED = str(os.getenv("IDENTIFY_STATION_PRIOR_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
_IDENTIFY_STATION_PRIOR_SEED_CONF = float(os.getenv("IDENTIFY_STATION_PRIOR_SEED_CONF", "0.72") or "0.72")
_IDENTIFY_STATION_PRIOR_SEED_GAP = float(os.getenv("IDENTIFY_STATION_PRIOR_SEED_GAP", "0.04") or "0.04")
_IDENTIFY_STATION_PRIOR_MAX_DELTA = float(os.getenv("IDENTIFY_STATION_PRIOR_MAX_DELTA", "0.06") or "0.06")


def _parse_rerank_angles() -> List[float]:
    raw = str(os.getenv("LABELER_RERANK_ANGLES", "-10,0,10") or "").strip()
    if not raw:
        return [0.0]
    out: List[float] = []
    seen: set[float] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = float(tok)
        except Exception:
            continue
        key = round(v, 4)
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    if not out:
        out = [0.0]
    has_zero = any(abs(x) < 1e-6 for x in out)
    if not has_zero:
        out.append(0.0)
    return out


_RERANK_ANGLES = _parse_rerank_angles()

def _parse_serial(val: str) -> Optional[int]:
    m = SN_PATTERN.search(val or "")
    if m:
        return int(m.group(1))
    if str(val or "").strip().isdigit():
        return int(str(val).strip())
    return None


def _parse_serial_crop_from_path(path: str) -> Tuple[Optional[int], Optional[int]]:
    raw = str(path or "")
    if not raw:
        return None, None
    serial = _parse_serial(raw)
    crop: Optional[int] = None
    base = os.path.basename(raw)
    m = _CROP_NUM_PATTERN.search(base)
    if m:
        try:
            g1 = m.group(1)
            g2 = m.group(2)
            crop = int(g1 or g2)
        except Exception:
            crop = None
    return serial, crop


def _cat_name_from_full(full_name: str) -> str:
    s = str(full_name or "").strip()
    if not s:
        return ""
    m = _CAT_ID_NAME_RE.match(s)
    if m:
        return m.group(2).strip()
    return s


def _profile_cat_names() -> List[str]:
    out: List[str] = []
    try:
        from ..services import profile_cache
        for full in profile_cache.all_actual_names():
            name = _cat_name_from_full(str(full))
            if name:
                key = re.sub(r"[^a-z0-9]+", "", name.lower())
                if key in {"notacat", "needsreview", "rejected"}:
                    continue
                out.append(name)
    except Exception:
        return out
    return out


def get_all_known_cats() -> List[str]:
    """Return union of CatDatabase names and gallery names."""
    names: set[str] = set()
    names.update(_profile_cat_names())
    if _gallery_names:
        names.update(_gallery_names)
    return sorted(names)


def _rebuild_gallery_cat_indices() -> None:
    """Build name -> embedding-index tensor lookup for fast per-cat rerank scoring."""
    global _gallery_cat_indices
    if _gallery_emb is None or not _gallery_names:
        _gallery_cat_indices = {}
        return
    by_cat: dict[str, List[int]] = {}
    for idx, name in enumerate(_gallery_names):
        by_cat.setdefault(str(name), []).append(int(idx))
    device = _gallery_emb.device
    _gallery_cat_indices = {
        name: torch.tensor(idxs, dtype=torch.long, device=device)
        for name, idxs in by_cat.items()
        if idxs
    }


def _sort_candidate_rows(rows: List[Tuple[str, float, float]]) -> List[Tuple[str, float, float]]:
    rows.sort(key=lambda item: (-float(item[1]), -float(item[2]), str(item[0] or "").lower()))
    return rows


def _clamp_confidence_score(score: Any) -> float:
    """Keep displayed/stored confidence-like scores within percentage bounds."""
    try:
        value = float(score)
    except Exception:
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _rank_unique_candidates_for_similarity(
    sims: Tensor,
    *,
    crop: Optional[Image.Image] = None,
    rerank: bool = True,
) -> List[Tuple[str, float, float]]:
    """Return one scored row per cat, optionally reranked, sorted best-first."""
    if not _gallery_names:
        return []
    rows: List[Tuple[str, float, float]] = []
    if _gallery_cat_indices:
        for cat_name, idxs in _gallery_cat_indices.items():
            label = str(cat_name or "").strip()
            if not label:
                continue
            try:
                cat_sims = sims.index_select(0, idxs)
            except Exception:
                continue
            if getattr(cat_sims, "numel", lambda: 0)() <= 0:
                continue
            try:
                base_conf = float(torch.max(cat_sims).item())
            except Exception:
                continue
            if not math.isfinite(base_conf):
                continue
            rows.append((label, base_conf, base_conf))
    else:
        vals, idxs = torch.sort(sims, descending=True)
        seen: set[str] = set()
        total = int(idxs.numel()) if hasattr(idxs, "numel") else len(_gallery_names)
        for j in range(total):
            try:
                cat_idx = int(idxs[j].item())
            except Exception:
                continue
            if cat_idx < 0 or cat_idx >= len(_gallery_names):
                continue
            cat_name = str(_gallery_names[cat_idx] or "").strip()
            if not cat_name or cat_name in seen:
                continue
            try:
                base_conf = float(vals[j].item())
            except Exception:
                continue
            if not math.isfinite(base_conf):
                continue
            rows.append((cat_name, base_conf, base_conf))
            seen.add(cat_name)

    if bool(rerank) and bool(_RERANK_ENABLED) and crop is not None and rows:
        rerank_pool = [name for name, _, _ in rows[: min(len(rows), int(_RERANK_TOP_N))]]
        reranked = _rerank_scores_for_crop(crop, rerank_pool)
        if reranked:
            updated: List[Tuple[str, float, float]] = []
            for name, _, base_conf in rows:
                score = float(reranked.get(name, base_conf))
                updated.append((name, score, base_conf))
            rows = updated

    return _sort_candidate_rows(rows)


def _hungarian_min_cost(cost_rows: List[List[float]]) -> List[int]:
    """Solve a rectangular min-cost assignment with rows <= cols."""
    if not cost_rows:
        return []
    n = len(cost_rows)
    m = max((len(row) for row in cost_rows), default=0)
    if n <= 0 or m <= 0:
        return [-1] * n
    if n > m:
        raise ValueError("Hungarian assignment requires columns >= rows")

    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = float(cost_rows[i0 - 1][j - 1]) - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * n
    for j in range(1, m + 1):
        if p[j] != 0:
            assignment[p[j] - 1] = j - 1
    return assignment


def _assign_unique_cat_names(
    candidate_rows_by_crop: List[List[Tuple[str, float, float]]],
) -> List[Optional[str]]:
    """Choose one unique cat per crop that maximizes total confidence."""
    if not candidate_rows_by_crop:
        return []

    label_names: List[str] = []
    label_to_idx: Dict[str, int] = {}
    max_score = float("-inf")
    min_score = float("inf")
    for rows in candidate_rows_by_crop:
        for name, score, _ in rows:
            cat_name = str(name or "").strip()
            if not cat_name:
                continue
            if cat_name not in label_to_idx:
                label_to_idx[cat_name] = len(label_names)
                label_names.append(cat_name)
            max_score = max(max_score, float(score))
            min_score = min(min_score, float(score))

    if not label_names:
        return [None] * len(candidate_rows_by_crop)

    if not math.isfinite(max_score):
        max_score = 0.0
    if not math.isfinite(min_score):
        min_score = 0.0
    missing_score = float(min_score) - max(1.0, abs(min_score) + abs(max_score) + 1.0)

    if len(label_names) < len(candidate_rows_by_crop):
        for idx in range(len(candidate_rows_by_crop) - len(label_names)):
            dummy_name = f"__dummy__{idx + 1}"
            label_to_idx[dummy_name] = len(label_names)
            label_names.append(dummy_name)

    score_rows: List[List[float]] = []
    for rows in candidate_rows_by_crop:
        score_row = [missing_score] * len(label_names)
        for name, score, _ in rows:
            cat_name = str(name or "").strip()
            if not cat_name:
                continue
            col = label_to_idx.get(cat_name)
            if col is None:
                continue
            score_row[col] = float(score)
        score_rows.append(score_row)

    score_ceiling = max(max(row) for row in score_rows) if score_rows else 0.0
    cost_rows = [
        [float(score_ceiling) - float(score) for score in row]
        for row in score_rows
    ]
    assignment = _hungarian_min_cost(cost_rows)

    out: List[Optional[str]] = []
    for row_idx, col_idx in enumerate(assignment):
        if 0 <= col_idx < len(label_names):
            assigned = str(label_names[col_idx] or "").strip()
            if assigned and not assigned.startswith("__dummy__"):
                out.append(assigned)
                continue
        fallback = next((str(name or "").strip() for name, _, _ in candidate_rows_by_crop[row_idx] if str(name or "").strip()), None)
        out.append(fallback or None)
    return out


def _visible_unique_candidate_rows(
    rows: List[Tuple[str, float, float]],
    *,
    assigned_name: Optional[str],
    taken_elsewhere: set[str],
) -> List[Tuple[str, float, float]]:
    """Filter out names reserved by other crops while keeping this crop's assignment first."""
    if not rows:
        return []
    assigned = str(assigned_name or "").strip()
    visible = [
        row
        for row in rows
        if str(row[0] or "").strip() and (str(row[0] or "").strip() == assigned or str(row[0] or "").strip() not in taken_elsewhere)
    ]
    if not assigned:
        return visible
    chosen = next((row for row in visible if str(row[0] or "").strip() == assigned), None)
    if chosen is None:
        return visible
    return [chosen] + [row for row in visible if str(row[0] or "").strip() != assigned]


def _collect_station_votes(
    candidate_rows_by_crop: List[List[Tuple[str, float, float]]],
    active_membership: Dict[str, List[str]],
) -> tuple[Dict[str, float], int]:
    station_votes: Dict[str, float] = {}
    contributing_crops = 0
    for rows in candidate_rows_by_crop:
        if not rows:
            continue
        top_name = str(rows[0][0] or "").strip()
        stations = list(active_membership.get(top_name) or [])
        if not stations:
            continue
        top_score = float(rows[0][1])
        second_score = float(rows[1][1]) if len(rows) > 1 else 0.0
        margin = max(0.0, top_score - second_score)
        if top_score < _IDENTIFY_STATION_PRIOR_SEED_CONF and margin < _IDENTIFY_STATION_PRIOR_SEED_GAP:
            continue
        weight = max(0.0, top_score - _IDENTIFY_STATION_PRIOR_SEED_CONF)
        weight += margin * 1.5
        if top_score >= _IDENTIFY_STATION_PRIOR_SEED_CONF and margin >= _IDENTIFY_STATION_PRIOR_SEED_GAP:
            weight += 0.2
        if weight <= 0.0:
            continue
        contributing_crops += 1
        per_station = float(weight) / max(1, len(stations))
        for station in stations:
            station_votes[station] = float(station_votes.get(station, 0.0)) + per_station
    return station_votes, contributing_crops


def _apply_identify_station_prior(
    candidate_rows_by_crop: List[List[Tuple[str, float, float]]],
) -> List[List[Tuple[str, float, float]]]:
    if not _IDENTIFY_STATION_PRIOR_ENABLED or len(candidate_rows_by_crop) < 2:
        return candidate_rows_by_crop
    active_membership = get_active_cat_station_membership()
    if not active_membership:
        return candidate_rows_by_crop
    station_votes, contributing_crops = _collect_station_votes(candidate_rows_by_crop, active_membership)
    if contributing_crops < 2 or not station_votes:
        return candidate_rows_by_crop
    best_vote = max(float(v) for v in station_votes.values())
    total_vote = sum(float(v) for v in station_votes.values())
    if best_vote <= 0.0 or total_vote <= 0.0:
        return candidate_rows_by_crop
    consensus = max(0.0, min(1.0, ((best_vote / total_vote) - 0.5) / 0.5))
    if consensus <= 0.0:
        return candidate_rows_by_crop
    support_strength = min(1.0, max(0.0, float(contributing_crops - 1) / 4.0))
    confidence_strength = min(1.0, best_vote / 1.2)
    prior_strength = support_strength * confidence_strength * consensus
    if prior_strength <= 0.0:
        return candidate_rows_by_crop

    adjusted_rows: List[List[Tuple[str, float, float]]] = []
    for rows in candidate_rows_by_crop:
        updated: List[Tuple[str, float, float]] = []
        for name, score, base_score in rows:
            stations = list(active_membership.get(str(name or "").strip()) or [])
            if not stations:
                updated.append((name, float(score), float(base_score)))
                continue
            candidate_support = max(float(station_votes.get(station, 0.0)) for station in stations)
            normalized_support = min(1.0, candidate_support / best_vote) if best_vote > 0.0 else 0.0
            delta = _IDENTIFY_STATION_PRIOR_MAX_DELTA * prior_strength * ((2.0 * normalized_support) - 1.0)
            updated.append((name, float(score) + float(delta), float(base_score)))
        adjusted_rows.append(_sort_candidate_rows(updated))
    return adjusted_rows

@dataclass
class IdentifyResult:
    boxed_jpeg: bytes
    results: List[dict]
    crops: List[bytes] = field(default_factory=list)
    image_size: Tuple[int, int] = (0, 0)

@dataclass
class Det:
    xyxy: Tuple[float, float, float, float]
    conf: float

#---------- Architecture Wrapper ----------
class DINOv3Wrapper(torch.nn.Module):
    """Matches the exact structure from your R4.5 notebook checkpoint."""
    def __init__(self):
        super().__init__()
        import timm
        # 1. Base Model (768 features)
        self.backbone = timm.create_model(
            'vit_base_patch16_dinov3', 
            pretrained=True,
            num_classes=0
        )
        
        # 2. Corrected Head Structure (Linear -> BN -> PReLU)
        self.head = torch.nn.Sequential(
            torch.nn.Linear(768, 512, bias=True),
            torch.nn.BatchNorm1d(512),
            torch.nn.PReLU()
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Silence flash attention warning for Windows
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*Torch was not compiled with flash attention.*")
            feat = self.backbone(x)
        emb = self.head(feat)
        return torch.nn.functional.normalize(emb, p=2, dim=1)

#---------- Device & Loader Helpers ----------
def _pick_device() -> torch.device:
    if torch.cuda.is_available(): return torch.device("cuda")
    return torch.device("cpu")

def _ensure_device_only() -> None:
    global _device, _half
    if _device is None:
        _device = _pick_device()
        _half = bool(settings.cv_half) and _device.type == "cuda"

def _ensure_detector() -> None:
    global _yolo
    _ensure_device_only()
    if _yolo is not None: return
    if YOLO is None: raise RuntimeError("ultralytics not installed")
    y: Any = YOLO(settings.cv_detect_weights)
    _yolo = y

def _ensure_sam() -> None:
    """Load SAM2 model for box refinement."""
    global _sam
    _ensure_device_only()
    global _sam_failed
    if _sam is not None or _sam_failed:
        return
    with _sam_lock:
        if _sam is not None or _sam_failed:
            return
        try:
            if SAM is None:
                raise RuntimeError("ultralytics SAM not available")
            _sam = SAM(settings.cv_sam_weights)
            # Ultralytics 8.3.249 calls predictor.model.warmup() unconditionally,
            # but SAM2Model does not implement it. Patch in a no-op so prompted
            # segmentation does not fail on first use.
            try:
                sam_model = getattr(_sam, "model", None)
                if sam_model is not None and not hasattr(sam_model, "warmup"):
                    setattr(sam_model, "warmup", lambda *args, **kwargs: None)
            except Exception:
                pass
            log_action("viz_sam_load", "sam_ready", settings.cv_sam_weights)
        except Exception as e:
            _sam_failed = True
            log_action("viz_sam_load_error", "error", str(e))
            _sam = None

def _ensure_classifier() -> None:
    """Load the DINOv3 encoder and the .pt gallery."""
    global _clf, _gallery_emb, _gallery_names, _gallery_paths, _gallery_records
    _ensure_device_only()
    if _clf is not None and _gallery_emb is not None: return

    try:
        #1. Load Encoder (.pth brain)
        encoder = DINOv3Wrapper()
        try:
            state = torch.load(settings.cv_encoder_weights, map_location=_device, weights_only=True)
        except:
            state = torch.load(settings.cv_encoder_weights, map_location=_device)

        encoder.load_state_dict(state, strict=True)
        encoder.to(_device).eval()
        _clf = encoder

        #2. Load Gallery (.pt memories)
        gallery_target = str(settings.cv_gallery_path or "").strip()
        gallery_path = Path(gallery_target)
        if gallery_target and (not gallery_path.exists() or gallery_path.stat().st_size == 0):
            fallback = _find_latest_local_gallery()
            if fallback and str(fallback) != gallery_target:
                gallery_target = fallback
                settings.cv_gallery_path = str(fallback)
                gallery_path = Path(gallery_target)
        if not gallery_path.exists() or gallery_path.stat().st_size == 0:
            raise RuntimeError(f"Gallery file is missing or empty: {gallery_target}. Please run a gallery retrain.")
        try:
            gal_data = torch.load(gallery_target, map_location=_device, weights_only=True)
        except Exception:
            gal_data = torch.load(gallery_target, map_location=_device, weights_only=False)

        _gallery_emb = (gal_data.get('emb') or gal_data['embeddings']).to(_device)
        _gallery_emb = torch.nn.functional.normalize(_gallery_emb, p=2, dim=1)

        idx_to_class = gal_data.get('idx_to_class') or {v: k for k, v in gal_data['class_to_idx'].items()}
        _gallery_names = [idx_to_class[int(i)] for i in (gal_data.get('label') or gal_data['labels'])]
        raw_paths = gal_data.get("path") or gal_data.get("paths") or gal_data.get("img_paths") or []
        raw_records = gal_data.get("records") or gal_data.get("gallery_records") or []
        _gallery_records, _gallery_paths = _build_gallery_runtime_metadata(
            _gallery_names,
            raw_paths,
            raw_records,
        )
        _rebuild_gallery_cat_indices()
        log_action("viz_clf_load_info", "reid_ready", f"cats={len(set(_gallery_names))}; gallery={gallery_target}")
    except Exception as e:
        log_action("viz_clf_load_error", f"type={type(e).__name__}", str(e))
        _clf = None

#---------- Image Processing Helpers ----------
def _enforce_max_dim(img: Image.Image) -> None:
    m = settings.cv_max_image_dim
    if not m: return
    w, h = img.size
    if w <= m and h <= m: return
    if w > h:
        nw, nh = m, int(h * (m / w))
    else:
        nw, nh = int(w * (m / h)), m
    img.draft(None, (nw, nh)) 

def _expand_box(x1: float, y1: float, x2: float, y2: float, pad_pct: float, img_w: int, img_h: int) -> Tuple[float, float, float, float]:
    w, h = x2 - x1, y2 - y1
    pw, ph = w * pad_pct, h * pad_pct
    return (
        max(0, x1 - pw),
        max(0, y1 - ph),
        min(img_w, x2 + pw),
        min(img_h, y2 + ph)
    )

def _prep_tensor(pil: Image.Image) -> Tensor:
    from torchvision.transforms import Compose, Resize, ToTensor, Normalize
    size = settings.cv_clf_imgsz
    tfm = Compose([
        Resize((size, size)),
        ToTensor(),
        Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    return cast(Tensor, tfm(pil))


def _rerank_variant_tensors(crop: Image.Image) -> List[Tensor]:
    """Build rotation variants, with optional mirroring, for rerank embedding."""
    variants: List[Tensor] = []
    for angle in _RERANK_ANGLES:
        if abs(float(angle)) < 1e-6:
            rotated = crop
        else:
            try:
                rotated = crop.rotate(
                    float(angle),
                    resample=Image.Resampling.BICUBIC,
                    expand=False,
                    fillcolor=(0, 0, 0),
                )
            except Exception:
                rotated = crop.rotate(float(angle), expand=False)
        base = _prep_tensor(rotated)
        variants.append(base)
        if _RERANK_HFLIP:
            variants.append(torch.flip(base, dims=[2]))
    return variants


def _rerank_scores_for_crop(crop: Image.Image, candidate_names: List[str]) -> dict[str, float]:
    """Return max similarity per candidate cat across rotation variants."""
    if not candidate_names:
        return {}
    if _clf is None or _gallery_emb is None:
        return {}

    variants = _rerank_variant_tensors(crop)
    if not variants:
        return {}

    batch = torch.stack(variants).to(_device)
    with torch.inference_mode():
        q = _clf(batch)

    out: dict[str, float] = {}
    for name in candidate_names:
        idxs = _gallery_cat_indices.get(name)
        if idxs is None or idxs.numel() == 0:
            continue
        cat_emb = _gallery_emb.index_select(0, idxs)
        sim = q @ cat_emb.T
        out[name] = float(torch.max(sim).item())
    return out

def _get_gallery_root_hints() -> List[Path]:
    """Return cached local roots to resolve gallery paths from training runs."""
    global _gallery_root_hints
    if _gallery_root_hints is not None:
        return _gallery_root_hints
    roots: List[Path] = []
    env_root = os.getenv("CV_GALLERY_LOCAL_ROOT", "").strip()
    if env_root:
        p = Path(env_root)
        if p.exists():
            roots.append(p)
    # Common local training data location used by the legacy classifier tool
    docs_root = (
        Path.home()
        / "Documents"
        / "TomCat VI Training"
        / "ClassifierModelTraining"
        / "ClassifierTrainingData"
        / "sortedPics"
        / "HITL_Crops"
    )
    if docs_root.exists():
        roots.append(docs_root)
    _gallery_root_hints = roots
    return roots

def _slug_text(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s or "cat"

def _parse_gallery_crop_uri(path: str) -> Tuple[str, str]:
    raw = str(path or "").strip()
    lower = raw.lower()
    if not (lower.startswith("sheet://") or lower.startswith("crop://")):
        return "", ""
    body = raw.split("://", 1)[1]
    if ":" in body:
        crop_id, cat_name = body.split(":", 1)
    else:
        crop_id, cat_name = body, ""
    return str(crop_id).strip(), str(cat_name).strip()


def _coerce_gallery_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _gallery_record_from_legacy_path(cat_name: str, path: str) -> dict[str, Any]:
    raw_path = str(path or "").strip()
    crop_id, cat_from_uri = _parse_gallery_crop_uri(raw_path)
    serial, crop_num = _parse_serial_crop_from_path(raw_path)
    name = str(cat_name or cat_from_uri or "").strip()
    if not raw_path and crop_id and name:
        raw_path = f"crop://{crop_id}:{name}"
    return {
        "cat_name": name,
        "serial": serial,
        "crop": crop_num,
        "crop_id": str(crop_id or "").strip(),
        "path": raw_path,
        "source": "legacy_path",
    }


def _normalize_gallery_record(
    raw_record: Any,
    *,
    fallback_name: str = "",
    fallback_path: str = "",
) -> dict[str, Any]:
    record = _gallery_record_from_legacy_path(str(fallback_name or ""), str(fallback_path or ""))
    if not isinstance(raw_record, dict):
        return record

    cat_name = str(
        raw_record.get("cat_name")
        or raw_record.get("name")
        or record.get("cat_name")
        or ""
    ).strip()
    crop_id = str(
        raw_record.get("crop_id")
        or raw_record.get("id")
        or record.get("crop_id")
        or ""
    ).strip()
    path = str(
        raw_record.get("path")
        or raw_record.get("gallery_path")
        or raw_record.get("crop_uri")
        or record.get("path")
        or ""
    ).strip()
    if not path and crop_id and cat_name:
        path = f"crop://{crop_id}:{cat_name}"

    serial = _coerce_gallery_int(raw_record.get("serial"))
    crop_num = _coerce_gallery_int(raw_record.get("crop"))
    if serial is None or crop_num is None:
        legacy = _gallery_record_from_legacy_path(cat_name, path)
        if serial is None:
            serial = _coerce_gallery_int(legacy.get("serial"))
        if crop_num is None:
            crop_num = _coerce_gallery_int(legacy.get("crop"))
        if not crop_id:
            crop_id = str(legacy.get("crop_id") or "").strip()

    source = str(raw_record.get("source") or record.get("source") or "").strip()
    return {
        "cat_name": cat_name,
        "serial": serial,
        "crop": crop_num,
        "crop_id": crop_id,
        "path": path,
        "source": source,
    }


def _build_gallery_runtime_metadata(
    names: List[str],
    raw_paths: Any,
    raw_records: Any,
) -> Tuple[List[dict[str, Any]], List[str]]:
    paths_in: List[str] = []
    if isinstance(raw_paths, (list, tuple)):
        paths_in = [str(p or "") for p in raw_paths]

    records_in: List[Any] = list(raw_records) if isinstance(raw_records, (list, tuple)) else []
    use_records = len(records_in) == len(names)

    records: List[dict[str, Any]] = []
    paths: List[str] = []
    for idx, name in enumerate(names):
        fallback_path = paths_in[idx] if idx < len(paths_in) else ""
        raw_record = records_in[idx] if use_records else None
        record = _normalize_gallery_record(
            raw_record,
            fallback_name=str(name or ""),
            fallback_path=fallback_path,
        )
        records.append(record)
        paths.append(str(record.get("path") or fallback_path or ""))
    return records, paths


def _gallery_record_for_index(abs_idx: int) -> dict[str, Any]:
    if 0 <= int(abs_idx) < len(_gallery_records):
        rec = _gallery_records[int(abs_idx)]
        if isinstance(rec, dict):
            return rec
    fallback_path = _gallery_paths[int(abs_idx)] if 0 <= int(abs_idx) < len(_gallery_paths) else ""
    fallback_name = _gallery_names[int(abs_idx)] if 0 <= int(abs_idx) < len(_gallery_names) else ""
    return _normalize_gallery_record(None, fallback_name=fallback_name, fallback_path=fallback_path)

def _get_gallery_crop_roots() -> List[Path]:
    """Return candidate local crop roots for resolving gallery crop URIs."""
    global _gallery_crop_roots
    if _gallery_crop_roots is not None:
        return _gallery_crop_roots
    roots: List[Path] = []
    active = Path("cache") / "gallery_retrain" / "active_crops"
    if active.exists():
        roots.append(active)
    work = Path("cache") / "gallery_retrain" / "work"
    if work.exists():
        runs = sorted(
            [p for p in work.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )
        for run in runs[:4]:
            crops = run / "crops"
            if crops.exists():
                roots.append(crops)
    _gallery_crop_roots = roots
    return roots

def _resolve_gallery_path(path: str) -> str:
    """Map gallery paths saved in training to local filesystem paths."""
    if not path:
        return path
    cached = _resolved_gallery_path_cache.get(path)
    if cached:
        return cached
    resolved = path
    crop_id, cat_name = _parse_gallery_crop_uri(path)
    if crop_id:
        slug = _slug_text(cat_name)
        for root in _get_gallery_crop_roots():
            # Current updater layout: <root>/<cat_slug>/<crop_id>.jpg
            candidate = root / slug / f"{crop_id}.jpg"
            if candidate.exists():
                resolved = str(candidate)
                _resolved_gallery_path_cache[path] = resolved
                return resolved
            # Back-compat for non-slugged cat folders.
            if cat_name:
                legacy = root / cat_name / f"{crop_id}.jpg"
                if legacy.exists():
                    resolved = str(legacy)
                    _resolved_gallery_path_cache[path] = resolved
                    return resolved
    try:
        if os.path.exists(path):
            resolved = path
            _resolved_gallery_path_cache[path] = resolved
            return resolved
    except Exception:
        pass
    # Typical Colab root
    if path.startswith("/content/"):
        if "/content/reid_data/" in path:
            suffix = path.split("/content/reid_data/", 1)[1]
        else:
            suffix = path.split("/content/", 1)[1]
        for root in _get_gallery_root_hints():
            candidate = root / suffix.replace("/", os.sep)
            if candidate.exists():
                resolved = str(candidate)
                _resolved_gallery_path_cache[path] = resolved
                return resolved
    _resolved_gallery_path_cache[path] = resolved
    return resolved

def _thumb_b64(path: str, size: int = 96) -> Optional[str]:
    """Load an image, generate a small JPEG thumbnail, and return base64."""
    cache_key = (str(path), int(size))
    cached = _thumb_cache.get(cache_key)
    if cached:
        return cached
    try:
        resolved = _resolve_gallery_path(path)
        img = _open_rgb_image(resolved)
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        out = base64.b64encode(buf.getvalue()).decode("ascii")
        _thumb_cache[cache_key] = out
        if len(_thumb_cache) > _thumb_cache_max:
            # Drop the oldest inserted key (dict preserves insertion order in Py3.7+).
            old_key = next(iter(_thumb_cache))
            _thumb_cache.pop(old_key, None)
        return out
    except Exception:
        return None

def _thumb_b64_from_pil(img: Image.Image, size: int = 96) -> Optional[str]:
    """Generate a small JPEG thumbnail from a PIL image and return base64."""
    try:
        thumb = img.copy()
        thumb.thumbnail((size, size))
        buf = io.BytesIO()
        thumb.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


_PIL_MAX_PIXELS = 500_000_000

def _open_rgb_image(source: Any) -> Image.Image:
    """Open an image and normalize EXIF orientation before RGB conversion."""
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = _PIL_MAX_PIXELS
    try:
        img = Image.open(source)
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img.convert("RGB")

def _make_collage(crops: List[Image.Image]) -> Image.Image:
    """Combine multiple crops into a single grid image."""
    if not crops: return Image.new('RGB', (100, 100), color='gray')
    if len(crops) == 1: return crops[0]
    
    count = len(crops)
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    
    w, h = crops[0].size
    grid = Image.new('RGB', (cols * w, rows * h), color='white')
    
    for i, c in enumerate(crops):
        c_resized = c.resize((w, h))
        x = (i % cols) * w
        y = (i // cols) * h
        grid.paste(c_resized, (x, y))
        
    return grid

#---------- Core Logic ----------
def _run_yolo(img: Image.Image) -> List[Det]:
    _ensure_detector()
    res = _yolo.predict(img, conf=settings.cv_conf or _DEFAULT_CONF, imgsz=settings.cv_detect_imgsz, verbose=False)
    dets = []
    for r in res:
        boxes = r.boxes.xyxy.detach().cpu().numpy()
        confs = r.boxes.conf.detach().cpu().numpy()
        for b, c in zip(boxes, confs):
            dets.append(Det((float(b[0]), float(b[1]), float(b[2]), float(b[3])), float(c)))
    return dets

def detect(image_bytes: bytes, *, include_boxed_image: bool = True) -> IdentifyResult:
    """Run detection only and return the boxed image."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    image_size = (int(img.size[0]), int(img.size[1]))
    dets = _run_yolo(img)
    boxed_jpeg = b""
    if include_boxed_image:
        annotated = _draw_boxes(img.copy(), dets)
        buf = io.BytesIO()
        annotated.save(buf, format="JPEG")
        boxed_jpeg = buf.getvalue()
    results = [{"box": d.xyxy, "conf": d.conf} for d in dets]
    return IdentifyResult(boxed_jpeg=boxed_jpeg, results=results, image_size=image_size)

def crop(image_bytes: bytes) -> IdentifyResult:
    """Run detection and return individual cropped cats plus a collage summary."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    dets = _run_yolo(img)
    
    crops = []
    results = []
    crop_jpegs: List[bytes] = []
    if dets:
        for d in dets:
            x1, y1, x2, y2 = d.xyxy
            cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, *img.size)
            crop_img = img.crop((cx1, cy1, cx2, cy2))
            crops.append(crop_img)
            results.append({"box": d.xyxy})
            buf = io.BytesIO()
            crop_img.save(buf, format="JPEG")
            crop_jpegs.append(buf.getvalue())
        final_img = _make_collage(crops)
    else:
        final_img = img

    buf = io.BytesIO()
    final_img.save(buf, format="JPEG")
    return IdentifyResult(boxed_jpeg=buf.getvalue(), results=results, crops=crop_jpegs)

def identify(image_bytes: bytes) -> IdentifyResult:
    """Run detection then 512D similarity search for identification."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    
    _ensure_classifier()
    
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)
    results = []

    if _clf is not None and _gallery_emb is not None and dets:
        tiles: List[Tensor] = []
        tile_crops: List[Image.Image] = []
        boxes: List[Tuple[int, int, int, int]] = []
        for d in dets:
            x1, y1, x2, y2 = d.xyxy
            cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, *img.size)
            crop = img.crop((cx1, cy1, cx2, cy2))
            tiles.append(_prep_tensor(crop))
            tile_crops.append(crop)
            boxes.append((int(cx1), int(cy1), int(cx2), int(cy2)))

        if tiles:
            batch = torch.stack(tiles).to(_device)
            with torch.inference_mode():
                query_embs = _clf(batch)
                similarities = query_embs @ _gallery_emb.T
            candidate_rows_by_crop = [
                _rank_unique_candidates_for_similarity(
                    similarities[i],
                    crop=tile_crops[i] if i < len(tile_crops) else None,
                    rerank=True,
                )
                for i in range(len(dets))
            ]
            candidate_rows_by_crop = _apply_identify_station_prior(candidate_rows_by_crop)
            assigned_names = _assign_unique_cat_names(candidate_rows_by_crop)

            for i in range(len(dets)):
                taken_elsewhere = {
                    str(name).strip()
                    for idx, name in enumerate(assigned_names)
                    if idx != i and str(name or "").strip()
                }
                visible_rows = _visible_unique_candidate_rows(
                    candidate_rows_by_crop[i],
                    assigned_name=assigned_names[i] if i < len(assigned_names) else None,
                    taken_elsewhere=taken_elsewhere,
                )
                top_candidates = [
                    (name, _clamp_confidence_score(score))
                    for name, score, _ in visible_rows[:5]
                    if str(name or "").strip()
                ]
                if not top_candidates:
                    continue
                best_name, best_conf = top_candidates[0]
                results.append({
                    "index": i + 1,
                    "name": best_name,
                    "conf": best_conf,
                    "box": boxes[i],
                    "top5": top_candidates,
                })

    buf = io.BytesIO()
    annotated.save(buf, format="JPEG")
    return IdentifyResult(boxed_jpeg=buf.getvalue(), results=results)

def _gallery_refs_for_candidate(
    *,
    cat_name: str,
    sims: Tensor,
    refs_per: int,
    thumb_size: int,
    include_thumb: bool = True,
    search_pool: Optional[int] = None,
) -> List[dict]:
    """Return top-k gallery refs (serial/crop metadata, optionally with thumbs)."""
    idxs = _gallery_cat_indices.get(cat_name)
    if idxs is None or idxs.numel() == 0:
        return []
    k_target = max(0, int(refs_per or 0))
    if k_target <= 0:
        return []
    try:
        cat_sims = sims.index_select(0, idxs)
        pool_target = max(k_target, int(search_pool or _LABELER_REF_SEARCH_POOL))
        pool_k = min(pool_target, int(cat_sims.numel()))
        if pool_k <= 0:
            return []
        topk = torch.topk(cat_sims, k=pool_k)
    except Exception:
        return []

    out: List[dict] = []
    seen_sc: set[Tuple[Optional[int], Optional[int]]] = set()
    seen_thumb: set[str] = set()
    seen_path: set[str] = set()
    top_indices = [int(x) for x in topk.indices.tolist()]
    top_values = [float(x) for x in topk.values.tolist()]
    for pos, rel in enumerate(top_indices):
        try:
            abs_idx = int(idxs[rel].item())
        except Exception:
            continue
        if abs_idx < 0:
            continue
        rec = _gallery_record_for_index(abs_idx)
        gpath = str(rec.get("path") or "")
        serial = _coerce_gallery_int(rec.get("serial"))
        crop_num = _coerce_gallery_int(rec.get("crop"))
        thumb = ""
        if include_thumb:
            thumb = (_thumb_b64(gpath, size=thumb_size) or "") if gpath else ""
            # If local thumb extraction fails but serial/crop metadata is known,
            # still keep this ref so downstream can serve it via ref_crop URL.
            if not thumb and (serial is None or crop_num is None):
                continue
        sc_key = (serial, crop_num)
        if serial is not None and crop_num is not None and sc_key in seen_sc:
            continue
        if include_thumb and thumb in seen_thumb:
            continue
        if (serial is None or crop_num is None) and gpath in seen_path:
            continue
        seen_sc.add(sc_key)
        if include_thumb and thumb:
            seen_thumb.add(thumb)
        if serial is None or crop_num is None:
            seen_path.add(gpath)
        row = {"serial": serial, "crop": crop_num}
        if pos < len(top_values):
            row["sim"] = float(top_values[pos])
        if include_thumb and thumb:
            row["img"] = thumb
        out.append(row)
        if len(out) >= k_target:
            break
    return out


def _merge_query_specific_refs(
    gallery_refs: List[dict],
    local_thumb_refs: List[dict],
    refs_per: int,
) -> List[dict]:
    """Keep DINO-ranked refs, but hydrate them with warmed local thumbs when possible."""
    target = max(0, int(refs_per or 0))
    if target <= 0:
        return []

    thumb_by_sc: dict[Tuple[int, int], dict] = {}
    for ref in local_thumb_refs or []:
        if not isinstance(ref, dict):
            continue
        try:
            serial = int(ref.get("serial"))
            crop_num = int(ref.get("crop"))
        except Exception:
            continue
        if serial <= 0 or crop_num <= 0:
            continue
        thumb_by_sc[(serial, crop_num)] = ref

    merged: List[dict] = []
    seen_sc: set[Tuple[int, int]] = set()
    seen_thumb: set[str] = set()

    def _append_ref(raw_ref: Any) -> None:
        if len(merged) >= target or not isinstance(raw_ref, dict):
            return
        row = dict(raw_ref)
        sc_key: Optional[Tuple[int, int]] = None
        try:
            serial = int(row.get("serial"))
            crop_num = int(row.get("crop"))
            if serial > 0 and crop_num > 0:
                sc_key = (serial, crop_num)
        except Exception:
            sc_key = None
        if sc_key is not None and sc_key in seen_sc:
            return
        thumb = str(row.get("img") or "").strip()
        if not thumb and sc_key is not None:
            thumb = str((thumb_by_sc.get(sc_key) or {}).get("img") or "").strip()
            if thumb:
                row["img"] = thumb
        if thumb and thumb in seen_thumb:
            return
        if sc_key is not None:
            seen_sc.add(sc_key)
        if thumb:
            seen_thumb.add(thumb)
        merged.append(row)

    for ref in gallery_refs or []:
        _append_ref(ref)
        if len(merged) >= target:
            return merged
    for ref in local_thumb_refs or []:
        _append_ref(ref)
        if len(merged) >= target:
            return merged
    return merged


def identify_boxes(
    image_bytes: bytes,
    boxes: List[Tuple[float, float, float, float]],
    *,
    top_k: int = 9,
    refs_per: int = 5,
    thumb_size: int = 128,
    rerank: bool = True,
    include_ref_thumbs: bool = True,
    enforce_unique_across_crops: bool = False,
    focus_crop_idx: Optional[int] = None,
    trace_tag: Optional[str] = None,
) -> IdentifyResult:
    """Run DINOv3 identification on specific normalized boxes (cx, cy, w, h)."""
    t0_total = time.perf_counter()
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    _ensure_classifier()
    preprocess_ms = (time.perf_counter() - t0_total) * 1000.0

    if _clf is None or _gallery_emb is None or not boxes:
        return IdentifyResult(boxed_jpeg=b"", results=[])

    img_w, img_h = img.size
    tiles: List[Tensor] = []
    tile_crops: List[Image.Image] = []
    valid_boxes: List[Tuple[int, int, int, int]] = []
    tile_prep_t0 = time.perf_counter()

    for box in boxes:
        try:
            cx, cy, w, h = [float(x) for x in box]
        except Exception:
            continue
        if w <= 0 or h <= 0:
            continue
        x1 = (cx - w / 2) * img_w
        y1 = (cy - h / 2) * img_h
        x2 = (cx + w / 2) * img_w
        y2 = (cy + h / 2) * img_h
        cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, img_w, img_h)
        crop = img.crop((cx1, cy1, cx2, cy2))
        tiles.append(_prep_tensor(crop))
        tile_crops.append(crop)
        valid_boxes.append((int(cx1), int(cy1), int(cx2), int(cy2)))
    tile_prep_ms = (time.perf_counter() - tile_prep_t0) * 1000.0

    if not tiles:
        return IdentifyResult(boxed_jpeg=b"", results=[])

    embed_t0 = time.perf_counter()
    batch = torch.stack(tiles).to(_device)
    with torch.inference_mode():
        query_embs = _clf(batch)
        similarities = query_embs @ _gallery_emb.T
    embed_ms = (time.perf_counter() - embed_t0) * 1000.0

    rank_t0 = time.perf_counter()
    candidate_rows_by_crop = [
        _rank_unique_candidates_for_similarity(
            similarities[i],
            crop=tile_crops[i] if i < len(tile_crops) else None,
            rerank=bool(rerank),
        )
        for i in range(similarities.shape[0])
    ]
    assigned_names = (
        _assign_unique_cat_names(candidate_rows_by_crop)
        if bool(enforce_unique_across_crops)
        else []
    )
    rank_ms = (time.perf_counter() - rank_t0) * 1000.0

    results: List[dict] = []
    focus_idx: Optional[int] = None
    try:
        if focus_crop_idx is not None:
            parsed_focus = int(focus_crop_idx)
            if 0 <= parsed_focus < int(similarities.shape[0]):
                focus_idx = parsed_focus
    except Exception:
        focus_idx = None
    ref_gallery_ms = 0.0
    ref_query_ms = 0.0
    result_build_t0 = time.perf_counter()
    ref_calls = 0
    candidate_name_total = 0
    returned_ref_total = 0
    for i in range(similarities.shape[0]):
        sims = similarities[i]
        if bool(enforce_unique_across_crops):
            taken_elsewhere = {
                str(name).strip()
                for idx, name in enumerate(assigned_names)
                if idx != i and str(name or "").strip()
            }
            visible_rows = _visible_unique_candidate_rows(
                candidate_rows_by_crop[i],
                assigned_name=assigned_names[i] if i < len(assigned_names) else None,
                taken_elsewhere=taken_elsewhere,
            )
            trimmed_rows = visible_rows[: int(top_k)]
        else:
            trimmed_rows = candidate_rows_by_crop[i][: int(top_k)]
        candidate_names = [name for name, _, _ in trimmed_rows]
        candidate_scores = [float(score) for _, score, _ in trimmed_rows]
        base_score_map = {name: float(base_score) for name, _, base_score in trimmed_rows}
        candidate_name_total += len(candidate_names)
        refs_per_i = max(0, int(refs_per or 0))
        if focus_idx is not None and i != focus_idx:
            refs_per_i = 0
        ref_lists: dict[str, List[dict]] = {n: [] for n in candidate_names}
        for name in candidate_names:
            refs: List[dict] = []
            if refs_per_i > 0:
                ref_calls += 1
                t_gallery_refs = time.perf_counter()
                refs = _gallery_refs_for_candidate(
                    cat_name=name,
                    sims=sims,
                    refs_per=refs_per_i,
                    thumb_size=thumb_size,
                    include_thumb=bool(include_ref_thumbs),
                    search_pool=_LABELER_REF_SEARCH_POOL,
                )
                ref_gallery_ms += (time.perf_counter() - t_gallery_refs) * 1000.0
                if _labeler_ref_ready:
                    t_query_refs = time.perf_counter()
                    extra = _get_labeler_refs_for_cat(name, query_embs[i], refs_per_i)
                    ref_query_ms += (time.perf_counter() - t_query_refs) * 1000.0
                    if extra:
                        refs = _merge_query_specific_refs(refs, list(extra), refs_per_i)
            ref_lists[name] = refs
            returned_ref_total += len(refs)

        candidates = []
        for name, conf in zip(candidate_names, candidate_scores):
            candidates.append({
                "name": name,
                "conf": _clamp_confidence_score(conf),
                "conf_base": _clamp_confidence_score(base_score_map.get(name, conf)),
                "refs": ref_lists.get(name, []),
            })

        results.append({
            "index": i + 1,
            "box": valid_boxes[i] if i < len(valid_boxes) else None,
            "candidates": candidates,
        })

    result_build_ms = (time.perf_counter() - result_build_t0) * 1000.0
    total_ms = (time.perf_counter() - t0_total) * 1000.0
    if trace_tag or total_ms >= 5000.0 or ref_gallery_ms >= 1500.0:
        log_action(
            "labeler_identify_boxes_profile",
            str(trace_tag or "trace=auto"),
            (
                f"total_ms={int(round(total_ms))}; preprocess_ms={int(round(preprocess_ms))}; "
                f"tile_prep_ms={int(round(tile_prep_ms))}; embed_ms={int(round(embed_ms))}; "
                f"rank_ms={int(round(rank_ms))}; gallery_refs_ms={int(round(ref_gallery_ms))}; "
                f"query_refs_ms={int(round(ref_query_ms))}; result_build_ms={int(round(result_build_ms))}; "
                f"crops={int(similarities.shape[0])}; candidate_names={int(candidate_name_total)}; "
                f"ref_calls={int(ref_calls)}; returned_refs={int(returned_ref_total)}; "
                f"thumbs={int(bool(include_ref_thumbs))}; rerank={int(bool(rerank))}; "
                f"focus_idx={focus_idx if focus_idx is not None else -1}; "
                f"labeler_ref_ready={int(bool(_labeler_ref_ready))}"
            ),
        )

    return IdentifyResult(boxed_jpeg=b"", results=results)

def _normalize_cat_label(label: str, cat_map: dict[str, str]) -> Optional[str]:
    lbl = (label or "").strip()
    if not lbl:
        return None
    low = lbl.lower()
    if low in {"needsreview", "rejected"}:
        return None
    if low in cat_map:
        return cat_map[low]
    # Strip numeric prefix like "1. Twix"
    if "." in lbl:
        prefix, rest = lbl.split(".", 1)
        if prefix.strip().isdigit():
            cand = rest.strip()
            cand_low = cand.lower()
            if cand_low in cat_map:
                return cat_map[cand_low]
    return None

def _parse_yolo_box_str(box_str: str) -> Optional[Tuple[float, float, float, float]]:
    try:
        parts = [float(p) for p in box_str.strip().split()]
    except Exception:
        return None
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]

def _embed_crops(crops: List[Image.Image]) -> Tensor:
    _ensure_classifier()
    if _clf is None:
        return torch.empty((0, 512))
    tensors = [_prep_tensor(c) for c in crops]
    if not tensors:
        return torch.empty((0, 512))
    batch_size = max(1, int(os.getenv("LABELER_REF_EMBED_BATCH_SIZE", "8") or "8"))
    out = []
    for i in range(0, len(tensors), batch_size):
        batch = torch.stack(tensors[i:i + batch_size]).to(_device)
        try:
            with torch.inference_mode():
                emb = _clf(batch)
            out.append(emb.detach().cpu())
        except RuntimeError as e:
            if "out of memory" not in str(e).lower():
                raise
            if _device is not None and _device.type == "cuda":
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            # Fallback: process one crop at a time to minimize peak VRAM.
            for t in tensors[i:i + batch_size]:
                single = t.unsqueeze(0).to(_device)
                with torch.inference_mode():
                    emb1 = _clf(single)
                out.append(emb1.detach().cpu())
    return torch.cat(out, dim=0) if out else torch.empty((0, 512))


def _prepare_labeler_ref_entry(
    sn: int,
    coord_str: str,
    crop_idx: int,
    thumb_size: int,
) -> Optional[Tuple[Image.Image, dict[str, Any]]]:
    from ..services import local_photos

    coord = _parse_yolo_box_str(coord_str)
    if coord is None:
        return None
    data = local_photos.read_local_photo_bytes(int(sn))
    if not data:
        return None

    img: Optional[Image.Image] = None
    try:
        img = _open_rgb_image(io.BytesIO(data))
        img_w, img_h = img.size
        cx, cy, w, h = coord
        x1 = (cx - w / 2) * img_w
        y1 = (cy - h / 2) * img_h
        x2 = (cx + w / 2) * img_w
        y2 = (cy + h / 2) * img_h
        cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, img_w, img_h)
        crop = img.crop((cx1, cy1, cx2, cy2)).copy()
        thumb_b64 = _thumb_b64_from_pil(crop, size=thumb_size)
        if not thumb_b64:
            crop.close()
            return None
        return crop, {"img": thumb_b64, "serial": sn, "crop": crop_idx}
    except Exception:
        return None
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def _collect_labeler_ref_entries(
    entries: List[Tuple[int, str, int]],
    *,
    thumb_size: int,
) -> Tuple[List[Image.Image], List[dict[str, Any]]]:
    from ..services import local_photos

    if not entries:
        return [], []
    try:
        local_photos.local_serials(force_refresh=False)
    except Exception:
        pass

    crops: List[Image.Image] = []
    refs: List[dict[str, Any]] = []
    worker_count = min(max(1, int(_LABELER_REF_BUILD_WORKERS or 1)), len(entries))
    if worker_count <= 1:
        for sn, coord_str, crop_idx in entries:
            result = _prepare_labeler_ref_entry(sn, coord_str, crop_idx, thumb_size)
            if result is None:
                continue
            crop, ref = result
            crops.append(crop)
            refs.append(ref)
        return crops, refs

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="labeler_ref_build") as pool:
        futures = [
            pool.submit(_prepare_labeler_ref_entry, sn, coord_str, crop_idx, thumb_size)
            for sn, coord_str, crop_idx in entries
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                continue
            if result is None:
                continue
            crop, ref = result
            crops.append(crop)
            refs.append(ref)
    return crops, refs

async def _build_ref_cache(
    *,
    max_per_cat: int,
    thumb_size: int,
    progress_hook: Optional[Callable[[int, int], None]] = None,
) -> dict[str, dict[str, Any]]:
    """Build a per-cat embedding+thumbnail cache from labeled local metadata rows."""
    await asyncio.to_thread(_ensure_classifier)
    cat_list = await asyncio.to_thread(get_all_cats)
    cat_map = {c.lower(): c for c in cat_list}

    from ..services.catsheets import get_photo_metadata_rows

    rows = await asyncio.to_thread(get_photo_metadata_rows, ttl_sec=60)
    samples: dict[str, List[Tuple[int, str, str, int]]] = {c: [] for c in cat_list}
    counts: dict[str, int] = {c: 0 for c in cat_list}

    for row in rows[1:]:
        if len(row) <= COL_SERIAL:
            continue
        sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
        if sn is None:
            continue
        box_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
        box_cat_ids = row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else ""
        if not box_coords or not box_cat_ids:
            continue
        if str(box_coords).strip().lower() == "rejected":
            continue
        coords = [c for c in str(box_coords).split("|") if c.strip()]
        labels = [l for l in str(box_cat_ids).split("|") if l.strip()]
        if not coords or not labels:
            continue
        limit = min(len(coords), len(labels))
        for i in range(limit):
            cat = _normalize_cat_label(labels[i], cat_map)
            if not cat:
                continue
            counts[cat] += 1
            entry = (sn, coords[i], i + 1)
            bucket = samples[cat]
            if len(bucket) < max_per_cat:
                bucket.append(entry)
            else:
                j = random.randint(1, counts[cat])
                if j <= max_per_cat:
                    replace_idx = random.randrange(max_per_cat)
                    bucket[replace_idx] = entry

    new_cache: dict[str, dict[str, Any]] = {}
    total = len(samples)
    built = 0
    for cat, entries in samples.items():
        if not entries:
            built += 1
            if progress_hook:
                try:
                    progress_hook(built, total)
                except Exception:
                    pass
            continue

        crops, refs = await asyncio.to_thread(
            _collect_labeler_ref_entries,
            entries,
            thumb_size=thumb_size,
        )
        if crops:
            try:
                emb = await asyncio.to_thread(_embed_crops, crops)
                if emb.numel() > 0:
                    new_cache[cat] = {"emb": emb, "refs": refs}
            finally:
                for crop in crops:
                    try:
                        crop.close()
                    except Exception:
                        pass
        built += 1
        if progress_hook:
            try:
                progress_hook(built, total)
            except Exception:
                pass

    return new_cache


async def warm_labeler_refs(force: bool = False) -> dict:
    """Warm per-cat reference cache from local photo metadata rows."""
    global _labeler_ref_ready, _labeler_ref_building, _labeler_ref_task, _labeler_ref_cache
    global _labeler_ref_progress_total, _labeler_ref_progress_built
    if _labeler_ref_building:
        return labeler_ref_status()
    if _labeler_ref_ready and not force:
        needs_upgrade = False
        has_serial_crop_meta = False
        try:
            for pack in _labeler_ref_cache.values():
                if not isinstance(pack, dict):
                    needs_upgrade = True
                    break
                if "refs" not in pack:
                    needs_upgrade = True
                    break
                refs = pack.get("refs") or []
                if refs and isinstance(refs[0], str):
                    needs_upgrade = True
                    break
                for ref in refs:
                    if not isinstance(ref, dict):
                        continue
                    if ref.get("serial") is not None or ref.get("crop") is not None:
                        has_serial_crop_meta = True
                        break
                if has_serial_crop_meta:
                    break
            if not needs_upgrade and _labeler_ref_cache and not has_serial_crop_meta:
                needs_upgrade = True
        except Exception:
            needs_upgrade = True
        if not needs_upgrade:
            if _labeler_ref_progress_total <= 0:
                _labeler_ref_progress_total = max(
                    len(_labeler_ref_cache),
                    len(set(_gallery_names)) if _gallery_names else 0,
                )
            _labeler_ref_progress_built = max(
                int(_labeler_ref_progress_built or 0),
                int(_labeler_ref_progress_total or 0),
            )
            return labeler_ref_status()

    async def _build() -> None:
        global _labeler_ref_ready, _labeler_ref_building, _labeler_ref_cache
        global _labeler_ref_progress_total, _labeler_ref_progress_built
        _labeler_ref_building = True
        _labeler_ref_progress_total = 0
        _labeler_ref_progress_built = 0
        try:
            max_per_cat = int(getattr(settings, "labeler_ref_per_cat", 250) or 250)
            thumb_size = int(getattr(settings, "labeler_ref_thumb_size", 128) or 128)

            def _on_progress(built: int, total: int) -> None:
                global _labeler_ref_progress_total, _labeler_ref_progress_built
                t = max(0, int(total or 0))
                b = max(0, int(built or 0))
                if t > 0:
                    _labeler_ref_progress_total = t
                    _labeler_ref_progress_built = min(b, t)
                else:
                    _labeler_ref_progress_built = b

            _labeler_ref_cache = await _build_ref_cache(
                max_per_cat=max_per_cat,
                thumb_size=thumb_size,
                progress_hook=_on_progress,
            )
            if _labeler_ref_progress_total <= 0:
                _labeler_ref_progress_total = max(
                    len(_labeler_ref_cache),
                    len(set(_gallery_names)) if _gallery_names else 0,
                )
            _labeler_ref_progress_built = int(_labeler_ref_progress_total)
            _labeler_ref_ready = True
        except Exception as e:
            log_action("labeler_ref_build_error", "error", str(e))
            _labeler_ref_ready = False
        finally:
            _labeler_ref_building = False

    _labeler_ref_task = asyncio.create_task(_build())
    return labeler_ref_status()


def labeler_ref_status() -> dict:
    total = int(_labeler_ref_progress_total or 0)
    if total <= 0:
        total = max(
            len(_labeler_ref_cache),
            len(set(_gallery_names)) if _gallery_names else 0,
        )
    built = int(_labeler_ref_progress_built or 0)
    if _labeler_ref_ready and built < total:
        built = total
    return {
        "ready": _labeler_ref_ready,
        "building": _labeler_ref_building,
        "cats": len(_labeler_ref_cache),
        "built": built,
        "total": total,
    }


async def warm_labeler_manual_refs(force: bool = False) -> dict:
    """Warm lightweight manual-review state without heavy per-cat image builds."""
    global _manual_ref_ready, _manual_ref_building, _manual_ref_task
    global _manual_ref_cache, _manual_ref_progress_total, _manual_ref_progress_built, _manual_ref_per_cat
    target_per_cat = max(1, int(getattr(settings, "labeler_manual_ref_per_cat", 50) or 50))
    if _manual_ref_building:
        return labeler_manual_ref_status()
    if _manual_ref_ready and not force and int(_manual_ref_per_cat or 0) == target_per_cat:
        return labeler_manual_ref_status()

    _manual_ref_building = True
    _manual_ref_ready = False
    _manual_ref_progress_total = max(0, int(_manual_ref_progress_total or 0))
    _manual_ref_progress_built = max(0, int(_manual_ref_progress_built or 0))

    async def _build() -> None:
        global _manual_ref_ready, _manual_ref_building, _manual_ref_cache
        global _manual_ref_progress_total, _manual_ref_progress_built, _manual_ref_per_cat
        _manual_ref_progress_total = 0
        _manual_ref_progress_built = 0
        try:
            known = await asyncio.to_thread(get_all_known_cats)
            total = len(known)
            _manual_ref_progress_total = int(total)
            _manual_ref_progress_built = 0
            await asyncio.to_thread(_ensure_classifier)
            _manual_ref_cache = {}
            _manual_ref_progress_built = int(total)
            _manual_ref_per_cat = target_per_cat
            _manual_ref_ready = True
        except Exception as e:
            log_action("labeler_manual_ref_build_error", "error", str(e))
            _manual_ref_ready = False
        finally:
            _manual_ref_building = False

    _manual_ref_task = asyncio.create_task(_build())
    return labeler_manual_ref_status()


def labeler_manual_ref_status() -> dict:
    total = int(_manual_ref_progress_total or 0)
    if total <= 0:
        try:
            total = len(_profile_cat_names())
        except Exception:
            total = 0
    built = int(_manual_ref_progress_built or 0)
    if _manual_ref_ready and built < total:
        built = total
    return {
        "ready": _manual_ref_ready,
        "building": _manual_ref_building,
        "cats": max(len(_manual_ref_cache), total),
        "built": built,
        "total": total,
        "per_cat": int(_manual_ref_per_cat or 0),
    }


def _get_labeler_refs_for_cat(cat: str, query_emb: Tensor, refs_per: int) -> List[dict]:
    pack = _labeler_ref_cache.get(cat)
    if not pack:
        return []
    emb: Tensor = pack.get("emb")
    refs: List[dict] = pack.get("refs", []) or []
    if not refs:
        thumbs: List[str] = pack.get("thumb", []) or []
        if thumbs:
            refs = [{"img": t, "serial": None, "crop": None} for t in thumbs]
    if emb is None or not refs or emb.numel() == 0:
        return []
    try:
        # Ensure shapes
        q = query_emb.detach().cpu().view(1, -1)
        sims = (emb @ q.T).squeeze(1)
        k = min(refs_per, sims.numel(), len(refs))
        if k <= 0:
            return []
        topk = torch.topk(sims, k=k).indices.tolist()
        return [refs[i] for i in topk if i < len(refs)]
    except Exception:
        return []


def _embed_query_from_box(
    image_bytes: bytes,
    box: Tuple[float, float, float, float],
) -> Optional[Tensor]:
    try:
        img = _open_rgb_image(io.BytesIO(image_bytes))
    except Exception:
        return None
    _enforce_max_dim(img)
    img_w, img_h = img.size
    try:
        cx, cy, w, h = [float(x) for x in box]
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    x1 = (cx - w / 2) * img_w
    y1 = (cy - h / 2) * img_h
    x2 = (cx + w / 2) * img_w
    y2 = (cy + h / 2) * img_h
    cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, img_w, img_h)
    crop = img.crop((cx1, cy1, cx2, cy2))
    emb = _embed_crops([crop])
    if emb.numel() == 0:
        return None
    return emb[0].detach().cpu()


def manual_review_candidates(
    image_bytes: bytes,
    box: Tuple[float, float, float, float],
    *,
    refs_per: int = 1,
    thumb_size: int = 96,
    query_ref_cat_limit: int = 0,
    gallery_ref_search_pool: Optional[int] = None,
    rerank: bool = False,
) -> List[dict]:
    """Return one ranked candidate row per gallery cat for manual review."""
    _ensure_classifier()
    if _clf is None or _gallery_emb is None:
        return []
    query = _embed_query_from_box(image_bytes, box)
    if query is None:
        return []
    if not _gallery_names:
        return []
    q = query.view(-1)
    if q.device != _gallery_emb.device:
        q = q.to(_gallery_emb.device)
    sims = (_gallery_emb @ q).view(-1)
    refs_per_i = max(0, int(refs_per or 0))
    query_ref_limit = max(0, int(query_ref_cat_limit or 0))
    rows = _rank_unique_candidates_for_similarity(
        sims,
        crop=None,
        rerank=bool(rerank),
    )
    out: List[dict] = []
    for rank, (cat_name, score, _base_score) in enumerate(rows):
        refs: List[dict] = []
        if refs_per_i > 0 and rank < query_ref_limit:
            try:
                refs = _gallery_refs_for_candidate(
                    cat_name=str(cat_name or ""),
                    sims=sims,
                    refs_per=refs_per_i,
                    thumb_size=max(48, int(thumb_size or 96)),
                    include_thumb=False,
                    search_pool=gallery_ref_search_pool,
                )
            except Exception:
                refs = []
        out.append({
            "name": str(cat_name or ""),
            "conf": _clamp_confidence_score(score),
            "refs": refs,
        })
    return out

#---------- Visualization Helpers ----------
def _get_font(size: int) -> Any:
    global _font
    if _font: return _font
    try:
        _font = ImageFont.truetype("arial.ttf", size)
    except:
        _font = ImageFont.load_default()
    return _font

def _draw_boxes(img: Image.Image, dets: List[Det]) -> Image.Image:
    draw = ImageDraw.Draw(img)
    fnt = _get_font(max(12, int(img.size[0] * 0.02)))
    for i, d in enumerate(dets):
        x1, y1, x2, y2 = d.xyxy
        draw.rectangle([x1, y1, x2, y2], outline=_PURPLE, width=3)
        text = f"#{i+1}"
        tw, th = draw.textbbox((0, 0), text, font=fnt)[2:]
        draw.rectangle([x1, y1 - th, x1 + tw + 4, y1], fill=_PURPLE)
        draw.text((x1 + 2, y1 - th), text, fill="white", font=fnt)
    return img

#---------- Labeler API Functions ----------
@dataclass
class DetectWithSamResult:
    """Result of YOLO+SAM detection for the labeling tool."""
    boxed_jpeg: bytes
    boxes: List[Tuple[float, float, float, float]]  #YOLO-refined boxes (x1,y1,x2,y2)

@dataclass
class RefineBoxesResult:
    """Detector-guided SAM refinement output for the labeler."""
    boxes: List[Tuple[float, float, float, float]]
    summary: Dict[str, Any]
    polygons: List[List[Tuple[float, float]]] = field(default_factory=list)
    mask_tiles: List[Dict[str, Any]] = field(default_factory=list)


def _box_area(box: Tuple[float, float, float, float] | List[float]) -> float:
    x1, y1, x2, y2 = [float(v) for v in box]
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _clip_box_xyxy(
    box: Tuple[float, float, float, float] | List[float],
    img_w: int,
    img_h: int,
) -> Tuple[float, float, float, float]:
    x1, y1, x2, y2 = [float(v) for v in box]
    x1 = max(0.0, min(float(img_w), x1))
    y1 = max(0.0, min(float(img_h), y1))
    x2 = max(0.0, min(float(img_w), x2))
    y2 = max(0.0, min(float(img_h), y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (x1, y1, x2, y2)


def _int_box_bounds(
    box: Tuple[float, float, float, float] | List[float],
    img_w: int,
    img_h: int,
) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = _clip_box_xyxy(box, img_w, img_h)
    ix1 = max(0, min(int(img_w), int(math.floor(x1))))
    iy1 = max(0, min(int(img_h), int(math.floor(y1))))
    ix2 = max(ix1, min(int(img_w), int(math.ceil(x2))))
    iy2 = max(iy1, min(int(img_h), int(math.ceil(y2))))
    return (ix1, iy1, ix2, iy2)


def _round_metric(value: Any, digits: int = 3) -> float:
    try:
        return round(float(value), digits)
    except Exception:
        return 0.0


def _fallback_sam_box_diag(
    count: int,
    reason: str,
    *,
    passes: int = 1,
) -> Dict[str, Any]:
    return {
        "passes": int(max(1, passes)),
        "boxes": int(max(0, count)),
        "accepted_boxes": 0,
        "fallback_boxes": int(max(0, count)),
        "clipped_boxes": 0,
        "guard_reject_boxes": 0,
        "candidate_masks": 0,
        "accepted_masks": 0,
        "selected": {"tight": 0, "iou": 0, "fallback": int(max(0, count))},
        "max_outside_guard_ratio": 0.0,
        "max_detector_mask_ratio": 0.0,
        "max_detector_coverage": 0.0,
        "max_area_ratio": 1.0,
        "max_edge_shift_ratio": 0.0,
        "samples": [
            {
                "box_index": 0,
                "selected": "fallback",
                "reason": str(reason or "fallback"),
            }
        ] if count > 0 else [],
    }


def _choose_guarded_sam_box(
    detector_box: Tuple[float, float, float, float],
    guard_box: Tuple[float, float, float, float],
    candidates: List[Dict[str, Any]],
    *,
    overlap_threshold: float = _LABELER_SAM_TIGHT_OVERLAP_RATIO,
) -> Tuple[Optional[Tuple[float, float, float, float]], Dict[str, Any]]:
    detector_area = _box_area(detector_box)
    detector_w = max(1.0, abs(float(detector_box[2]) - float(detector_box[0])))
    detector_h = max(1.0, abs(float(detector_box[3]) - float(detector_box[1])))
    detector_max_dim = max(detector_w, detector_h)
    detector_cx = (float(detector_box[0]) + float(detector_box[2])) / 2.0
    detector_cy = (float(detector_box[1]) + float(detector_box[3])) / 2.0
    best_tight: Optional[Tuple[float, float, float, float]] = None
    best_tight_area = float("inf")
    best_tight_diag: Optional[Dict[str, Any]] = None
    best_iou = -1.0
    best_iou_box: Optional[Tuple[float, float, float, float]] = None
    best_iou_diag: Optional[Dict[str, Any]] = None
    best_preview_iou = -1.0
    best_preview_diag: Optional[Dict[str, Any]] = None
    accepted_masks = 0
    guard_rejections = 0

    for cand in candidates:
        cand_box = tuple(float(v) for v in (cand.get("box") or detector_box))
        cand_area = _box_area(cand_box)
        outside_guard_ratio = float(cand.get("outside_guard_ratio", 0.0) or 0.0)
        detector_mask_ratio = float(cand.get("detector_mask_ratio", 0.0) or 0.0)
        detector_coverage = float(cand.get("detector_coverage", 0.0) or 0.0)
        area_ratio = (
            float(cand.get("area_ratio", 0.0) or 0.0)
            if detector_area > 0
            else 0.0
        )
        cand_cx = (float(cand_box[0]) + float(cand_box[2])) / 2.0
        cand_cy = (float(cand_box[1]) + float(cand_box[3])) / 2.0
        center_shift_px = ((cand_cx - detector_cx) ** 2 + (cand_cy - detector_cy) ** 2) ** 0.5
        edge_shift_px = max(
            abs(float(cand_box[0]) - float(detector_box[0])),
            abs(float(cand_box[1]) - float(detector_box[1])),
            abs(float(cand_box[2]) - float(detector_box[2])),
            abs(float(cand_box[3]) - float(detector_box[3])),
        )
        edge_shift_ratio = edge_shift_px / detector_max_dim if detector_max_dim > 0 else 0.0
        cand["center_shift_px"] = center_shift_px
        cand["edge_shift_px"] = edge_shift_px
        cand["edge_shift_ratio"] = edge_shift_ratio
        ix1 = max(float(cand_box[0]), float(detector_box[0]))
        iy1 = max(float(cand_box[1]), float(detector_box[1]))
        ix2 = min(float(cand_box[2]), float(detector_box[2]))
        iy2 = min(float(cand_box[3]), float(detector_box[3]))
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        overlap_ratio = inter / detector_area if detector_area > 0 else 0.0
        union = cand_area + detector_area - inter
        iou = inter / union if union > 0 else 0.0
        if iou > best_preview_iou:
            best_preview_iou = iou
            best_preview_diag = cand

        allowed = (
            outside_guard_ratio <= float(_LABELER_SAM_MAX_OUTSIDE_GUARD_RATIO)
            and detector_mask_ratio >= float(_LABELER_SAM_MIN_DETECTOR_MASK_RATIO)
            and detector_coverage >= float(_LABELER_SAM_MIN_DETECTOR_COVERAGE)
            and area_ratio <= float(_LABELER_SAM_MAX_REFINED_AREA_RATIO)
            and edge_shift_ratio <= float(_LABELER_SAM_MAX_EDGE_SHIFT_RATIO)
        )
        if not allowed:
            guard_rejections += 1
            continue

        accepted_masks += 1

        if overlap_ratio >= overlap_threshold and cand_area < best_tight_area:
            best_tight = cand_box
            best_tight_area = cand_area
            best_tight_diag = cand

        if iou > best_iou:
            best_iou = iou
            best_iou_box = cand_box
            best_iou_diag = cand

    chosen_box = best_tight if best_tight is not None else best_iou_box
    chosen_diag = best_tight_diag if best_tight_diag is not None else best_iou_diag
    if chosen_box is None or chosen_diag is None:
        preview_diag = best_preview_diag if best_preview_diag is not None else {}
        return None, {
            "candidate_masks": int(len(candidates)),
            "accepted_masks": int(accepted_masks),
            "guard_rejections": int(guard_rejections),
            "selected_source": "fallback",
            "fallback_reason": "no_accepted_masks" if candidates else "no_masks",
            "clipped": False,
            # Do not surface a rejected preview mask as the accepted overlay.
            "polygon": [],
            "mask_tile": {},
            "preview_box": tuple(float(v) for v in (preview_diag.get("box") or detector_box))
            if preview_diag.get("box") is not None else None,
            "preview_polygon": list(preview_diag.get("polygon") or []),
            "preview_mask_tile": dict(preview_diag.get("mask_tile") or {}),
            "preview_iou": _round_metric(best_preview_iou if best_preview_iou >= 0.0 else 0.0),
            "outside_guard_ratio": 0.0,
            "detector_mask_ratio": 0.0,
            "detector_coverage": 0.0,
            "area_ratio": 1.0,
            "center_shift_px": 0.0,
            "edge_shift_px": 0.0,
            "edge_shift_ratio": 0.0,
            "preview_outside_guard_ratio": _round_metric(preview_diag.get("outside_guard_ratio", 0.0)),
            "preview_detector_mask_ratio": _round_metric(preview_diag.get("detector_mask_ratio", 0.0)),
            "preview_detector_coverage": _round_metric(preview_diag.get("detector_coverage", 0.0)),
            "preview_area_ratio": _round_metric(preview_diag.get("area_ratio", 1.0)),
            "preview_edge_shift_ratio": _round_metric(preview_diag.get("edge_shift_ratio", 0.0)),
        }

    return chosen_box, {
        "candidate_masks": int(len(candidates)),
        "accepted_masks": int(accepted_masks),
        "guard_rejections": int(guard_rejections),
        "selected_source": "tight" if best_tight is not None else "iou",
        "fallback_reason": "",
        "clipped": bool(chosen_diag.get("clipped")),
        "polygon": list(chosen_diag.get("polygon") or []),
        "mask_tile": dict(chosen_diag.get("mask_tile") or {}),
        "outside_guard_ratio": _round_metric(chosen_diag.get("outside_guard_ratio", 0.0)),
        "detector_mask_ratio": _round_metric(chosen_diag.get("detector_mask_ratio", 0.0)),
        "detector_coverage": _round_metric(chosen_diag.get("detector_coverage", 0.0)),
        "area_ratio": _round_metric(chosen_diag.get("area_ratio", 1.0)),
        "center_shift_px": _round_metric(chosen_diag.get("center_shift_px", 0.0), digits=2),
        "edge_shift_px": _round_metric(chosen_diag.get("edge_shift_px", 0.0), digits=2),
        "edge_shift_ratio": _round_metric(chosen_diag.get("edge_shift_ratio", 0.0)),
    }


def _split_sam_masks_by_prompt(results: List[Any], num_prompts: int) -> List[Any]:
    import numpy as np
    prompt_masks: List[Any] = []
    valid_results = [r for r in list(results or []) if getattr(r, "masks", None) is not None]
    if not valid_results:
        return [np.empty((0, 0, 0), dtype=bool) for _ in range(max(0, num_prompts))]

    if len(valid_results) == num_prompts:
        for r in valid_results:
            masks = getattr(getattr(r, "masks", None), "data", None)
            if masks is None:
                prompt_masks.append(np.empty((0, 0, 0), dtype=bool))
                continue
            prompt_masks.append(masks.detach().cpu().numpy())
        return prompt_masks

    all_mask_batches = []
    for r in valid_results:
        masks = getattr(getattr(r, "masks", None), "data", None)
        if masks is None:
            continue
        all_mask_batches.append(masks.detach().cpu().numpy())
    if not all_mask_batches:
        return [np.empty((0, 0, 0), dtype=bool) for _ in range(max(0, num_prompts))]

    all_masks = (
        all_mask_batches[0]
        if len(all_mask_batches) == 1
        else np.concatenate(all_mask_batches, axis=0)
    )
    total_masks = int(all_masks.shape[0]) if getattr(all_masks, "ndim", 0) >= 1 else 0
    if total_masks <= 0:
        return [np.empty((0, 0, 0), dtype=bool) for _ in range(max(0, num_prompts))]

    masks_per_prompt = max(1, total_masks // max(1, num_prompts))
    for i in range(max(0, num_prompts)):
        start_idx = i * masks_per_prompt
        if start_idx >= total_masks:
            prompt_masks.append(np.empty((0, *all_masks.shape[-2:]), dtype=bool))
            continue
        if i == (num_prompts - 1):
            end_idx = total_masks
        else:
            end_idx = min(total_masks, (i + 1) * masks_per_prompt)
        prompt_masks.append(all_masks[start_idx:end_idx])
    return prompt_masks


def _summarize_sam_refine_diags(diags: List[Dict[str, Any]], *, passes: int) -> Dict[str, Any]:
    total = len(diags)
    selected_tight = sum(1 for d in diags if str(d.get("selected_source") or "") == "tight")
    selected_iou = sum(1 for d in diags if str(d.get("selected_source") or "") == "iou")
    fallback = sum(1 for d in diags if str(d.get("selected_source") or "") == "fallback")
    clipped = sum(1 for d in diags if bool(d.get("clipped")))
    guard_reject_boxes = sum(1 for d in diags if int(d.get("guard_rejections") or 0) > 0)
    candidate_masks = sum(int(d.get("candidate_masks") or 0) for d in diags)
    accepted_masks = sum(int(d.get("accepted_masks") or 0) for d in diags)
    max_outside = max((_round_metric(d.get("outside_guard_ratio", 0.0)) for d in diags), default=0.0)
    max_detector_mask = max((_round_metric(d.get("detector_mask_ratio", 0.0)) for d in diags), default=0.0)
    max_detector_coverage = max((_round_metric(d.get("detector_coverage", 0.0)) for d in diags), default=0.0)
    max_area_ratio = max((_round_metric(d.get("area_ratio", 1.0)) for d in diags), default=1.0)
    max_edge_shift_ratio = max((_round_metric(d.get("edge_shift_ratio", 0.0)) for d in diags), default=0.0)

    samples: List[Dict[str, Any]] = []
    for d in diags:
        if len(samples) >= 3:
            break
        if (
            str(d.get("selected_source") or "") == "fallback"
            or bool(d.get("clipped"))
            or int(d.get("guard_rejections") or 0) > 0
        ):
            sample = {
                "box_index": int(d.get("box_index") or 0),
                "selected": str(d.get("selected_source") or "fallback"),
                "reason": str(d.get("fallback_reason") or ""),
                "candidate_masks": int(d.get("candidate_masks") or 0),
                "accepted_masks": int(d.get("accepted_masks") or 0),
                "guard_rejections": int(d.get("guard_rejections") or 0),
                "clipped": bool(d.get("clipped")),
                "outside_guard_ratio": _round_metric(d.get("outside_guard_ratio", 0.0)),
                "detector_mask_ratio": _round_metric(d.get("detector_mask_ratio", 0.0)),
                "detector_coverage": _round_metric(d.get("detector_coverage", 0.0)),
                "area_ratio": _round_metric(d.get("area_ratio", 1.0)),
                "edge_shift_ratio": _round_metric(d.get("edge_shift_ratio", 0.0)),
            }
            if str(d.get("selected_source") or "") == "fallback":
                sample["preview_iou"] = _round_metric(d.get("preview_iou", 0.0))
                sample["preview_detector_mask_ratio"] = _round_metric(d.get("preview_detector_mask_ratio", 0.0))
                sample["preview_detector_coverage"] = _round_metric(d.get("preview_detector_coverage", 0.0))
                sample["preview_area_ratio"] = _round_metric(d.get("preview_area_ratio", 1.0))
                sample["preview_edge_shift_ratio"] = _round_metric(d.get("preview_edge_shift_ratio", 0.0))
            samples.append(sample)

    return {
        "passes": int(max(1, passes)),
        "boxes": int(total),
        "accepted_boxes": int(total - fallback),
        "fallback_boxes": int(fallback),
        "clipped_boxes": int(clipped),
        "guard_reject_boxes": int(guard_reject_boxes),
        "candidate_masks": int(candidate_masks),
        "accepted_masks": int(accepted_masks),
        "selected": {
            "tight": int(selected_tight),
            "iou": int(selected_iou),
            "fallback": int(fallback),
        },
        "max_outside_guard_ratio": _round_metric(max_outside),
        "max_detector_mask_ratio": _round_metric(max_detector_mask),
        "max_detector_coverage": _round_metric(max_detector_coverage),
        "max_area_ratio": _round_metric(max_area_ratio),
        "max_edge_shift_ratio": _round_metric(max_edge_shift_ratio),
        "samples": samples,
    }


def _expand_box_with_guard(
    box: Tuple[float, float, float, float] | List[float],
    pad_pct: float,
    guard_box: Tuple[float, float, float, float] | List[float],
    img_w: int,
    img_h: int,
) -> Tuple[float, float, float, float]:
    x1, y1, x2, y2 = _expand_box(
        float(box[0]),
        float(box[1]),
        float(box[2]),
        float(box[3]),
        float(pad_pct),
        int(img_w),
        int(img_h),
    )
    gx1, gy1, gx2, gy2 = _clip_box_xyxy(guard_box, img_w, img_h)
    out = (
        max(float(gx1), float(x1)),
        max(float(gy1), float(y1)),
        min(float(gx2), float(x2)),
        min(float(gy2), float(y2)),
    )
    if out[2] <= out[0] or out[3] <= out[1]:
        return _clip_box_xyxy(guard_box, img_w, img_h)
    return out


def _extract_sam_masks(results: Any) -> Any:
    import numpy as np
    valid_results = [r for r in list(results or []) if getattr(r, "masks", None) is not None]
    if not valid_results:
        return np.empty((0, 0, 0), dtype=bool)
    first = valid_results[0]
    masks = getattr(getattr(first, "masks", None), "data", None)
    if masks is None:
        return np.empty((0, 0, 0), dtype=bool)
    return masks.detach().cpu().numpy()


def _mask_to_abs_polygon(
    mask: Any,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    max_points: int = 96,
) -> List[Tuple[float, float]]:
    import numpy as np

    if getattr(mask, "size", 0) == 0:
        return []

    points_raw: List[Tuple[float, float]] = []
    try:
        import cv2  # type: ignore

        mask_u8 = np.asarray(mask, dtype=np.uint8)
        if mask_u8.ndim != 2:
            return []
        if int(mask_u8.max() or 0) <= 1:
            mask_u8 = mask_u8 * 255
        contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            contour = max(contours, key=cv2.contourArea)
            approx = contour
            peri = float(cv2.arcLength(contour, True) or 0.0)
            if peri > 0.0:
                maybe = cv2.approxPolyDP(contour, max(1.0, peri * 0.003), True)
                if maybe is not None and len(maybe) >= 3:
                    approx = maybe
            pts = approx.reshape(-1, 2).tolist()
            points_raw = [(float(x), float(y)) for x, y in pts]
    except Exception:
        points_raw = []

    if len(points_raw) < 3:
        coords = np.argwhere(np.asarray(mask, dtype=bool))
        if coords.size <= 0:
            return []
        min_y, min_x = coords.min(axis=0)
        max_y, max_x = coords.max(axis=0)
        points_raw = [
            (float(min_x), float(min_y)),
            (float(max_x + 1), float(min_y)),
            (float(max_x + 1), float(max_y + 1)),
            (float(min_x), float(max_y + 1)),
        ]

    if len(points_raw) > int(max_points):
        step = max(1, int(math.ceil(len(points_raw) / float(max_points))))
        points_raw = points_raw[::step]
    return [
        (float(offset_x) + float(x), float(offset_y) + float(y))
        for x, y in points_raw
    ]


def _mask_crop_to_overlay_tile(
    mask: Any,
    *,
    local_box: Tuple[float, float, float, float],
    offset_x: float = 0.0,
    offset_y: float = 0.0,
) -> Dict[str, Any]:
    import numpy as np

    if getattr(mask, "size", 0) == 0:
        return {}
    try:
        from PIL import Image

        x1, y1, x2, y2 = [int(round(float(v))) for v in local_box]
        mask_u8 = np.asarray(mask, dtype=np.uint8)
        if mask_u8.ndim != 2:
            return {}
        if int(mask_u8.max() or 0) <= 1:
            mask_u8 = mask_u8 * 255
        crop = mask_u8[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
        if getattr(crop, "size", 0) <= 0:
            return {}

        rgba = np.zeros((crop.shape[0], crop.shape[1], 4), dtype=np.uint8)
        rgba[..., 0] = 74
        rgba[..., 1] = 158
        rgba[..., 2] = 255
        rgba[..., 3] = np.where(crop > 0, 50, 0).astype(np.uint8)
        img = Image.fromarray(rgba, mode="RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return {
            "x1": float(offset_x) + float(x1),
            "y1": float(offset_y) + float(y1),
            "x2": float(offset_x) + float(x2),
            "y2": float(offset_y) + float(y2),
            "png_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
        }
    except Exception:
        return {}


def _sam_refine_one_box(
    img_array: Any,
    prompt_box: Tuple[float, float, float, float],
    *,
    detector_box: Tuple[float, float, float, float],
    guard_box: Tuple[float, float, float, float],
) -> Tuple[Tuple[float, float, float, float], Dict[str, Any]]:
    import numpy as np
    img_h = int(getattr(img_array, "shape", [0, 0])[0] or 0)
    img_w = int(getattr(img_array, "shape", [0, 0])[1] or 0)
    gx1, gy1, gx2, gy2 = _int_box_bounds(guard_box, img_w, img_h)
    if gx2 <= gx1 or gy2 <= gy1:
        return detector_box, {
            "candidate_masks": 0,
            "accepted_masks": 0,
            "guard_rejections": 0,
            "selected_source": "fallback",
            "fallback_reason": "invalid_guard_crop",
            "clipped": False,
            "outside_guard_ratio": 0.0,
            "detector_mask_ratio": 0.0,
            "detector_coverage": 0.0,
            "area_ratio": 1.0,
        }

    crop = img_array[gy1:gy2, gx1:gx2]
    if getattr(crop, "size", 0) == 0:
        return detector_box, {
            "candidate_masks": 0,
            "accepted_masks": 0,
            "guard_rejections": 0,
            "selected_source": "fallback",
            "fallback_reason": "empty_guard_crop",
            "clipped": False,
            "outside_guard_ratio": 0.0,
            "detector_mask_ratio": 0.0,
            "detector_coverage": 0.0,
            "area_ratio": 1.0,
        }

    rel_prompt = (
        float(prompt_box[0]) - float(gx1),
        float(prompt_box[1]) - float(gy1),
        float(prompt_box[2]) - float(gx1),
        float(prompt_box[3]) - float(gy1),
    )
    rel_detector = (
        float(detector_box[0]) - float(gx1),
        float(detector_box[1]) - float(gy1),
        float(detector_box[2]) - float(gx1),
        float(detector_box[3]) - float(gy1),
    )
    crop_h = int(getattr(crop, "shape", [0, 0])[0] or 0)
    crop_w = int(getattr(crop, "shape", [0, 0])[1] or 0)
    rel_prompt = _clip_box_xyxy(rel_prompt, crop_w, crop_h)
    rel_detector = _clip_box_xyxy(rel_detector, crop_w, crop_h)

    try:
        with torch.inference_mode():
            results = _sam(crop, bboxes=[list(rel_prompt)], verbose=False)
    except Exception as e:
        log_action("viz_sam_box_error", "error", str(e))
        return detector_box, {
            "candidate_masks": 0,
            "accepted_masks": 0,
            "guard_rejections": 0,
            "selected_source": "fallback",
            "fallback_reason": "sam_error",
            "clipped": False,
            "outside_guard_ratio": 0.0,
            "detector_mask_ratio": 0.0,
            "detector_coverage": 0.0,
            "area_ratio": 1.0,
        }

    masks_data = _extract_sam_masks(results)
    if getattr(masks_data, "size", 0) == 0:
        return detector_box, {
            "candidate_masks": 0,
            "accepted_masks": 0,
            "guard_rejections": 0,
            "selected_source": "fallback",
            "fallback_reason": "no_masks",
            "clipped": False,
            "outside_guard_ratio": 0.0,
            "detector_mask_ratio": 0.0,
            "detector_coverage": 0.0,
            "area_ratio": 1.0,
        }

    dx1, dy1, dx2, dy2 = _int_box_bounds(rel_detector, crop_w, crop_h)
    detector_area = max(1.0, _box_area(rel_detector))
    candidates: List[Dict[str, Any]] = []
    for mask_idx in range(int(masks_data.shape[0])):
        mask = masks_data[mask_idx].astype(bool)
        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        if not (np.any(rows) and np.any(cols)):
            continue

        detector_pixels = int(np.count_nonzero(mask[dy1:dy2, dx1:dx2]))
        mask_area = int(np.count_nonzero(mask))
        if mask_area <= 0:
            continue

        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        local_box = (
            float(cmin),
            float(rmin),
            float(cmax + 1),
            float(rmax + 1),
        )
        abs_box = (
            float(gx1) + local_box[0],
            float(gy1) + local_box[1],
            float(gx1) + local_box[2],
            float(gy1) + local_box[3],
        )
        area_ratio = _box_area(local_box) / detector_area if detector_area > 0 else 0.0
        detector_mask_ratio = float(detector_pixels) / float(mask_area)
        detector_coverage = float(detector_pixels) / float(detector_area)
        candidates.append({
            "box": abs_box,
            "polygon": _mask_to_abs_polygon(mask, offset_x=float(gx1), offset_y=float(gy1)),
            "mask_tile": _mask_crop_to_overlay_tile(
                mask,
                local_box=local_box,
                offset_x=float(gx1),
                offset_y=float(gy1),
            ),
            "outside_guard_ratio": 0.0,
            "detector_mask_ratio": detector_mask_ratio,
            "detector_coverage": detector_coverage,
            "area_ratio": area_ratio,
            "clipped": False,
        })

    chosen_box, chosen_diag = _choose_guarded_sam_box(
        detector_box,
        guard_box,
        candidates,
        overlap_threshold=float(_LABELER_SAM_TIGHT_OVERLAP_RATIO),
    )
    if chosen_box is None:
        return detector_box, chosen_diag
    return _clip_box_xyxy(chosen_box, img_w, img_h), chosen_diag


def _sam_refine_boxes_batch(
    img_array: Any,
    prompt_boxes: List[List[float]],
    *,
    detector_boxes: Optional[List[List[float]]] = None,
    guard_boxes: Optional[List[List[float]]] = None,
) -> Tuple[
    List[Tuple[float, float, float, float]],
    List[Dict[str, Any]],
    List[List[Tuple[float, float]]],
    List[Dict[str, Any]],
]:
    """Use SAM to refine detector boxes while keeping outputs inside guard rails."""
    _ensure_sam()
    det_boxes = [
        [float(v) for v in (detector_boxes[i] if detector_boxes and i < len(detector_boxes) else prompt_boxes[i])]
        for i in range(len(prompt_boxes))
    ]
    grd_boxes = [
        [float(v) for v in (guard_boxes[i] if guard_boxes and i < len(guard_boxes) else det_boxes[i])]
        for i in range(len(prompt_boxes))
    ]
    if _sam is None:
        diags = [
            {
                "box_index": int(i),
                "candidate_masks": 0,
                "accepted_masks": 0,
                "guard_rejections": 0,
                "selected_source": "fallback",
                "fallback_reason": "sam_unavailable",
                "clipped": False,
                "polygon": [],
                "outside_guard_ratio": 0.0,
                "detector_mask_ratio": 0.0,
                "detector_coverage": 0.0,
                "area_ratio": 1.0,
            }
            for i in range(len(det_boxes))
        ]
        return [tuple(pb) for pb in det_boxes], diags, [[] for _ in range(len(det_boxes))], [{} for _ in range(len(det_boxes))]
    refined_boxes: List[Tuple[float, float, float, float]] = []
    polygons: List[List[Tuple[float, float]]] = []
    mask_tiles: List[Dict[str, Any]] = []
    diags: List[Dict[str, Any]] = []
    img_h = int(getattr(img_array, "shape", [0, 0])[0] or 0)
    img_w = int(getattr(img_array, "shape", [0, 0])[1] or 0)

    for i, prompt_box in enumerate(prompt_boxes):
        detector_box = _clip_box_xyxy(det_boxes[i], img_w, img_h)
        guard_box = _clip_box_xyxy(grd_boxes[i], img_w, img_h)
        chosen_box, chosen_diag = _sam_refine_one_box(
            img_array,
            tuple(float(v) for v in prompt_box),
            detector_box=detector_box,
            guard_box=guard_box,
        )
        refined_boxes.append(chosen_box)
        polygons.append([
            (float(x), float(y))
            for x, y in list(chosen_diag.get("polygon") or [])
        ])
        mask_tiles.append(dict(chosen_diag.get("mask_tile") or {}))
        chosen_diag["box_index"] = int(i)
        diags.append(chosen_diag)

    if _device is not None and _device.type == "cuda":
        torch.cuda.empty_cache()

    return refined_boxes, diags, polygons, mask_tiles


def _refine_absolute_boxes_with_diagnostics(
    img: Image.Image,
    img_array: Any,
    boxes: List[Tuple[float, float, float, float]],
    *,
    passes: int = 1,
) -> RefineBoxesResult:
    img_w, img_h = img.size
    detector_boxes = [
        _clip_box_xyxy(box, img_w, img_h)
        for box in list(boxes or [])
        if _box_area(box) > 0
    ]
    if not detector_boxes:
        return RefineBoxesResult(boxes=[], summary=_fallback_sam_box_diag(0, "no_boxes", passes=passes))

    guard_boxes = [
        _expand_box(
            box[0],
            box[1],
            box[2],
            box[3],
            float(_LABELER_SAM_GUARD_PAD_PCT),
            img_w,
            img_h,
        )
        for box in detector_boxes
    ]
    current_prompt_boxes = [
        _expand_box_with_guard(
            box,
            float(_LABELER_SAM_PROMPT_PAD_PCT),
            guard_boxes[i],
            img_w,
            img_h,
        )
        for i, box in enumerate(detector_boxes)
    ]

    last_diags: List[Dict[str, Any]] = []
    last_polygons: List[List[Tuple[float, float]]] = [[] for _ in range(len(detector_boxes))]
    last_mask_tiles: List[Dict[str, Any]] = [{} for _ in range(len(detector_boxes))]
    current_boxes = list(detector_boxes)
    pass_count = max(1, int(passes or 1))
    for pass_idx in range(pass_count):
        current_boxes, diags, polygons, mask_tiles = _sam_refine_boxes_batch(
            img_array,
            [list(b) for b in current_prompt_boxes],
            detector_boxes=[list(b) for b in detector_boxes],
            guard_boxes=[list(b) for b in guard_boxes],
        )
        for diag in diags:
            diag["pass"] = int(pass_idx + 1)
        last_diags = diags
        last_polygons = polygons
        last_mask_tiles = mask_tiles
        current_prompt_boxes = [
            _expand_box_with_guard(
                current_boxes[i],
                float(_LABELER_SAM_PROMPT_PAD_PCT),
                guard_boxes[i],
                img_w,
                img_h,
            )
            for i in range(len(current_boxes))
        ]

    return RefineBoxesResult(
        boxes=[_clip_box_xyxy(box, img_w, img_h) for box in current_boxes],
        summary=_summarize_sam_refine_diags(last_diags, passes=pass_count),
        polygons=list(last_polygons or []),
        mask_tiles=list(last_mask_tiles or []),
    )

def detect_with_sam(image_bytes: bytes) -> DetectWithSamResult:
    """Run YOLO detection then refine all boxes in a batched SAM call."""
    import numpy as np
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    img_array = np.array(img)
    
    dets = _run_yolo(img)
    if not dets:
        return DetectWithSamResult(boxed_jpeg=image_bytes, boxes=[])

    refine_result = _refine_absolute_boxes_with_diagnostics(
        img,
        img_array,
        [d.xyxy for d in dets],
        passes=1,
    )
    refined_boxes = refine_result.boxes

    #Draw refined boxes
    refined_dets = [Det(xyxy=b, conf=d.conf) for b, d in zip(refined_boxes, dets)]
    annotated = _draw_boxes(img.copy(), refined_dets)
    
    buf = io.BytesIO()
    annotated.save(buf, format="JPEG")
    return DetectWithSamResult(boxed_jpeg=buf.getvalue(), boxes=refined_boxes)

def refine_boxes(
    image_bytes: bytes,
    boxes: List[Tuple[float, float, float, float]],
    *,
    passes: int = 1,
) -> List[Tuple[float, float, float, float]]:
    """Refine provided YOLO-normalized boxes with SAM in batches; returns absolute xyxy boxes."""
    return refine_boxes_with_diagnostics(image_bytes, boxes, passes=passes).boxes


def refine_boxes_with_diagnostics(
    image_bytes: bytes,
    boxes: List[Tuple[float, float, float, float]],
    *,
    passes: int = 1,
) -> RefineBoxesResult:
    """Refine YOLO-normalized boxes with detector-guided SAM and return diagnostics."""
    if not boxes:
        return RefineBoxesResult(boxes=[], summary=_fallback_sam_box_diag(0, "no_boxes", passes=passes))

    import numpy as np
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    img_array = np.array(img)
    img_w, img_h = img.size

    absolute_boxes: List[Tuple[float, float, float, float]] = []
    for box in boxes:
        try:
            cx, cy, w, h = [float(x) for x in box]
        except Exception:
            continue
        if w <= 0 or h <= 0:
            continue
        x1 = (cx - w / 2) * img_w
        y1 = (cy - h / 2) * img_h
        x2 = (cx + w / 2) * img_w
        y2 = (cy + h / 2) * img_h
        absolute_boxes.append((x1, y1, x2, y2))

    if not absolute_boxes:
        return RefineBoxesResult(boxes=[], summary=_fallback_sam_box_diag(0, "invalid_boxes", passes=passes))

    return _refine_absolute_boxes_with_diagnostics(
        img,
        img_array,
        absolute_boxes,
        passes=passes,
    )


def warm_labeler_detector() -> dict:
    """Preload detector/SAM and run one tiny pass to avoid first-request cold start."""
    t0 = time.perf_counter()
    try:
        img = Image.new("RGB", (384, 384), color=(32, 32, 32))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        detect_with_sam(buf.getvalue())
        return {
            "ready": True,
            "sec": round(time.perf_counter() - t0, 3),
        }
    except Exception as e:
        log_action("labeler_detector_warm_error", f"type={type(e).__name__}", str(e))
        return {
            "ready": False,
            "sec": round(time.perf_counter() - t0, 3),
            "error": str(e),
        }

def get_all_cats() -> List[str]:
    """Return sorted list of all unique cat names from the gallery."""
    _ensure_classifier()
    if not _gallery_names:
        return []
    return sorted(set(_gallery_names))


def refresh_gallery(path: Optional[str] = None) -> dict:
    """Reload gallery tensors from disk without waiting for process restart."""
    global _gallery_emb, _gallery_names, _gallery_paths, _gallery_records, _labeler_ref_cache, _labeler_ref_ready, _labeler_ref_task
    global _labeler_ref_progress_total, _labeler_ref_progress_built
    global _manual_ref_cache, _manual_ref_ready, _manual_ref_task, _manual_ref_progress_total, _manual_ref_progress_built, _manual_ref_per_cat
    global _thumb_cache, _resolved_gallery_path_cache, _gallery_crop_roots
    _ensure_device_only()
    try:
        if path:
            settings.cv_gallery_path = str(path)
        target = str(settings.cv_gallery_path or "").strip()
        target_path = Path(target)
        if target and (not target_path.exists() or target_path.stat().st_size == 0):
            fallback = _find_latest_local_gallery()
            if fallback and str(fallback) != target:
                target = fallback
                settings.cv_gallery_path = str(fallback)
                target_path = Path(target)
        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError(f"Gallery file is missing or empty: {target}. Please run a gallery retrain.")
        try:
            gal_data = torch.load(target, map_location=_device, weights_only=True)
        except Exception:
            gal_data = torch.load(target, map_location=_device, weights_only=False)

        emb = (gal_data.get("emb") or gal_data["embeddings"]).to(_device)
        emb = torch.nn.functional.normalize(emb, p=2, dim=1)
        idx_to_class = gal_data.get("idx_to_class") or {v: k for k, v in gal_data["class_to_idx"].items()}
        labels = gal_data.get("label") or gal_data["labels"]
        names = [idx_to_class[int(i)] for i in labels]
        raw_paths = gal_data.get("path") or gal_data.get("paths") or gal_data.get("img_paths") or []
        raw_records = gal_data.get("records") or gal_data.get("gallery_records") or []
        records, paths = _build_gallery_runtime_metadata(names, raw_paths, raw_records)

        _gallery_emb = emb
        _gallery_names = names
        _gallery_paths = paths
        _gallery_records = records
        _rebuild_gallery_cat_indices()
        # Force ref cache rebuild against newest gallery.
        _labeler_ref_cache = {}
        _labeler_ref_ready = False
        _labeler_ref_task = None
        _labeler_ref_progress_total = 0
        _labeler_ref_progress_built = 0
        _manual_ref_cache = {}
        _manual_ref_ready = False
        _manual_ref_task = None
        _manual_ref_progress_total = 0
        _manual_ref_progress_built = 0
        _manual_ref_per_cat = 0
        _thumb_cache = {}
        _resolved_gallery_path_cache = {}
        _gallery_crop_roots = None

        return {
            "ok": True,
            "path": str(target),
            "embeddings": int(emb.shape[0]),
            "cats": int(len(set(names))),
            "records": int(len(records)),
        }
    except Exception as e:
        log_action("viz_gallery_refresh_error", "error", str(e))
        return {"ok": False, "error": str(e)}
