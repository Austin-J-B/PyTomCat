"""Utilities for running YOLO detection and DINOv3 ReID similarity for TomCat."""

from __future__ import annotations
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
from dataclasses import dataclass
from typing import List, Tuple, Optional, Any, Callable, cast

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

from ..config import settings
from ..logger import log_action

#---------- Constants ----------
_PURPLE = "#4C007F"
_DEFAULT_CONF = 0.552

#---------- Internal State ----------
_yolo: Optional[Any] = None
_sam: Optional[Any] = None
_sam_lock = threading.Lock()
_sam_failed: bool = False
_clf: Optional[torch.nn.Module] = None
_gallery_emb: Optional[Tensor] = None
_gallery_names: List[str] = []
_gallery_paths: List[str] = []
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
COL_URL = 6
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
_RERANK_HFLIP = str(os.getenv("LABELER_RERANK_HFLIP", "1")).strip().lower() in {"1", "true", "yes", "on"}
_LABELER_REF_SEARCH_POOL = max(5, int(os.getenv("LABELER_REF_SEARCH_POOL", "250") or "250"))


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

@dataclass
class IdentifyResult:
    boxed_jpeg: bytes
    results: List[dict]

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
            log_action("viz_sam_load", "sam_ready", settings.cv_sam_weights)
        except Exception as e:
            _sam_failed = True
            log_action("viz_sam_load_error", "error", str(e))
            _sam = None

def _ensure_classifier() -> None:
    """Load the DINOv3 encoder and the .pt gallery."""
    global _clf, _gallery_emb, _gallery_names, _gallery_paths
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
        try:
            gal_data = torch.load(settings.cv_gallery_path, map_location=_device, weights_only=False)
        except Exception:
            gal_data = torch.load(settings.cv_gallery_path, map_location=_device)

        _gallery_emb = gal_data['emb'].to(_device)
        _gallery_emb = torch.nn.functional.normalize(_gallery_emb, p=2, dim=1)
        
        idx_to_class = {v: k for k, v in gal_data['class_to_idx'].items()}
        _gallery_names = [idx_to_class[int(i)] for i in gal_data['label']]
        raw_paths = gal_data.get("path") or gal_data.get("paths") or gal_data.get("img_paths") or []
        if isinstance(raw_paths, (list, tuple)) and len(raw_paths) == _gallery_emb.shape[0]:
            _gallery_paths = [str(p) for p in raw_paths]
        else:
            _gallery_paths = []
        _rebuild_gallery_cat_indices()
        log_action("viz_clf_load_info", "reid_ready", f"cats={len(set(_gallery_names))}")
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
    """Build rotation/hflip variants for rerank embedding."""
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

def detect(image_bytes: bytes) -> IdentifyResult:
    """Run detection only and return the boxed image."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)
    
    buf = io.BytesIO()
    annotated.save(buf, format="JPEG")
    results = [{"box": d.xyxy, "conf": d.conf} for d in dets]
    return IdentifyResult(boxed_jpeg=buf.getvalue(), results=results)

def crop(image_bytes: bytes) -> IdentifyResult:
    """Run detection and return a collage of the cropped cats."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    dets = _run_yolo(img)
    
    crops = []
    results = []
    if dets:
        for d in dets:
            x1, y1, x2, y2 = d.xyxy
            cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, *img.size)
            crops.append(img.crop((cx1, cy1, cx2, cy2)))
            results.append({"box": d.xyxy})
        final_img = _make_collage(crops)
    else:
        final_img = img

    buf = io.BytesIO()
    final_img.save(buf, format="JPEG")
    return IdentifyResult(boxed_jpeg=buf.getvalue(), results=results)

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
                
                # Sort all matches descending (best matches first)
                vals, idxs = torch.sort(similarities, dim=1, descending=True)
                
                for i in range(len(dets)):
                    base_limit = max(5, int(_RERANK_TOP_N if _RERANK_ENABLED else 5))
                    candidate_names: List[str] = []
                    candidate_scores: List[float] = []
                    seen_cats: set[str] = set()
                    
                    # Iterate until we find unique identities for base + rerank pool
                    for j in range(len(_gallery_names)):
                        if len(candidate_names) >= base_limit:
                            break
                            
                        cat_idx = int(idxs[i, j])
                        cat_conf = float(vals[i, j])
                        cat_name = _gallery_names[cat_idx]
                        
                        if cat_name not in seen_cats:
                            candidate_names.append(cat_name)
                            candidate_scores.append(cat_conf)
                            seen_cats.add(cat_name)

                    base_score_map = {n: float(s) for n, s in zip(candidate_names, candidate_scores)}
                    if _RERANK_ENABLED and candidate_names and i < len(tile_crops):
                        rerank_pool = candidate_names[: min(len(candidate_names), int(_RERANK_TOP_N))]
                        reranked = _rerank_scores_for_crop(tile_crops[i], rerank_pool)
                        if reranked:
                            combined: List[Tuple[str, float, float]] = []
                            for name in candidate_names:
                                base_conf = base_score_map.get(name, 0.0)
                                score = float(reranked.get(name, base_conf))
                                combined.append((name, score, base_conf))
                            combined.sort(key=lambda x: x[1], reverse=True)
                            candidate_names = [n for (n, _, _) in combined]
                            candidate_scores = [float(s) for (_, s, _) in combined]
                            base_score_map = {n: float(b) for (n, _, b) in combined}

                    candidate_names = candidate_names[:5]
                    candidate_scores = candidate_scores[:5]
                    top_candidates = [(n, float(s)) for n, s in zip(candidate_names, candidate_scores)]
                    if not top_candidates:
                        continue
                    
                    # Best match is just the first unique one
                    best_name, best_conf = top_candidates[0]
                    
                    results.append({
                        "index": i + 1,
                        "name": best_name,
                        "conf": best_conf,
                        "box": boxes[i],
                        "top5": top_candidates
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
        if abs_idx < 0 or abs_idx >= len(_gallery_paths):
            continue
        gpath = _gallery_paths[abs_idx]
        serial, crop_num = _parse_serial_crop_from_path(gpath)
        thumb = ""
        if include_thumb:
            thumb = _thumb_b64(gpath, size=thumb_size) or ""
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


def identify_boxes(
    image_bytes: bytes,
    boxes: List[Tuple[float, float, float, float]],
    *,
    top_k: int = 9,
    refs_per: int = 5,
    thumb_size: int = 128,
    rerank: bool = True,
    include_ref_thumbs: bool = True,
) -> IdentifyResult:
    """Run DINOv3 identification on specific normalized boxes (cx, cy, w, h)."""
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    _ensure_classifier()

    if _clf is None or _gallery_emb is None or not boxes:
        return IdentifyResult(boxed_jpeg=b"", results=[])

    img_w, img_h = img.size
    tiles: List[Tensor] = []
    tile_crops: List[Image.Image] = []
    valid_boxes: List[Tuple[int, int, int, int]] = []

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

    if not tiles:
        return IdentifyResult(boxed_jpeg=b"", results=[])

    batch = torch.stack(tiles).to(_device)
    with torch.inference_mode():
        query_embs = _clf(batch)
        similarities = query_embs @ _gallery_emb.T

    results: List[dict] = []
    for i in range(similarities.shape[0]):
        sims = similarities[i]
        vals, idxs = torch.sort(sims, descending=True)

        use_rerank = bool(rerank) and bool(_RERANK_ENABLED)
        base_limit = max(int(top_k), int(_RERANK_TOP_N if use_rerank else top_k))
        candidate_names: List[str] = []
        candidate_scores: List[float] = []
        seen: set[str] = set()

        for j in range(len(_gallery_names)):
            cat_idx = int(idxs[j])
            cat_name = _gallery_names[cat_idx]
            if cat_name in seen:
                continue
            candidate_names.append(cat_name)
            candidate_scores.append(float(vals[j]))
            seen.add(cat_name)
            if len(candidate_names) >= base_limit:
                break

        base_score_map = {n: float(s) for n, s in zip(candidate_names, candidate_scores)}
        if use_rerank and candidate_names and i < len(tile_crops):
            rerank_pool = candidate_names[: min(len(candidate_names), int(_RERANK_TOP_N))]
            reranked = _rerank_scores_for_crop(tile_crops[i], rerank_pool)
            if reranked:
                combined: List[Tuple[str, float, float]] = []
                for name in candidate_names:
                    base_conf = base_score_map.get(name, 0.0)
                    score = float(reranked.get(name, base_conf))
                    combined.append((name, score, base_conf))
                combined.sort(key=lambda x: x[1], reverse=True)
                candidate_names = [n for (n, _, _) in combined]
                candidate_scores = [float(s) for (_, s, _) in combined]
                base_score_map = {n: float(b) for (n, _, b) in combined}

        candidate_names = candidate_names[: int(top_k)]
        candidate_scores = candidate_scores[: int(top_k)]
        refs_per_i = max(0, int(refs_per or 0))
        ref_lists: dict[str, List[dict]] = {n: [] for n in candidate_names}
        for name in candidate_names:
            refs: List[dict] = []
            if refs_per_i > 0:
                refs = _gallery_refs_for_candidate(
                    cat_name=name,
                    sims=sims,
                    refs_per=refs_per_i,
                    thumb_size=thumb_size,
                    include_thumb=bool(include_ref_thumbs),
                    search_pool=_LABELER_REF_SEARCH_POOL,
                )
                # Legacy fallback is only useful in thumb mode.
                if (
                    include_ref_thumbs
                    and _labeler_ref_ready
                    and len(refs) < refs_per_i
                ):
                    extra = _get_labeler_refs_for_cat(name, query_embs[i], refs_per_i)
                    if extra:
                        seen_sc: set[Tuple[Optional[int], Optional[int]]] = set()
                        seen_thumb: set[str] = set()
                        merged: List[dict] = []
                        for r in refs + list(extra):
                            if not isinstance(r, dict):
                                continue
                            serial = r.get("serial")
                            crop_num = r.get("crop")
                            thumb = str(r.get("img") or "").strip()
                            if not thumb:
                                continue
                            if serial is not None and crop_num is not None:
                                sc_key = (serial, crop_num)
                                if sc_key in seen_sc:
                                    continue
                                seen_sc.add(sc_key)
                            if thumb in seen_thumb:
                                continue
                            seen_thumb.add(thumb)
                            merged.append(r)
                            if len(merged) >= refs_per_i:
                                break
                        refs = merged
            ref_lists[name] = refs

        candidates = []
        for name, conf in zip(candidate_names, candidate_scores):
            candidates.append({
                "name": name,
                "conf": conf,
                "conf_base": float(base_score_map.get(name, conf)),
                "refs": ref_lists.get(name, []),
            })

        results.append({
            "index": i + 1,
            "box": valid_boxes[i] if i < len(valid_boxes) else None,
            "candidates": candidates,
        })

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

    from ..services.catsheets import get_tcb_pics_rows
    from ..services import labeler_cache, local_photos

    rows = get_tcb_pics_rows(ttl_sec=60)
    samples: dict[str, List[Tuple[int, str, str, int]]] = {c: [] for c in cat_list}
    counts: dict[str, int] = {c: 0 for c in cat_list}

    for row in rows[1:]:
        if len(row) <= COL_SERIAL:
            continue
        sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
        if sn is None:
            continue
        url = row[COL_URL] if len(row) > COL_URL else ""
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
            entry = (sn, url, coords[i], i + 1)
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

        crops: List[Image.Image] = []
        refs: List[dict] = []
        for sn, url, coord_str, crop_idx in entries:
            coord = _parse_yolo_box_str(coord_str)
            if coord is None:
                continue
            data = local_photos.read_local_photo_bytes(int(sn))
            if not data and str(url or "").startswith("http"):
                data = await labeler_cache.get_or_download(sn, url)
            if not data:
                continue
            try:
                img = _open_rgb_image(io.BytesIO(data))
            except Exception:
                continue
            img_w, img_h = img.size
            cx, cy, w, h = coord
            x1 = (cx - w / 2) * img_w
            y1 = (cy - h / 2) * img_h
            x2 = (cx + w / 2) * img_w
            y2 = (cy + h / 2) * img_h
            cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, img_w, img_h)
            crop = img.crop((cx1, cy1, cx2, cy2))
            thumb_b64 = _thumb_b64_from_pil(crop, size=thumb_size)
            if not thumb_b64:
                continue
            crops.append(crop)
            refs.append({"img": thumb_b64, "serial": sn, "crop": crop_idx})
        if crops:
            emb = await asyncio.to_thread(_embed_crops, crops)
            if emb.numel() > 0:
                new_cache[cat] = {"emb": emb, "refs": refs}
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
) -> List[dict]:
    """Score gallery cats against one crop for manual review selection."""
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
    refs_per_i = max(1, int(refs_per or 1))
    thumb_px = max(48, int(thumb_size or 96))
    out: List[dict] = []

    for cat in sorted(set(_gallery_names)):
        idxs = _gallery_cat_indices.get(cat)
        if idxs is None or idxs.numel() == 0:
            continue
        try:
            cat_sims = sims.index_select(0, idxs)
            k = min(refs_per_i, int(cat_sims.numel()))
            if k <= 0:
                continue
            topk = torch.topk(cat_sims, k=k)
            rel_idxs = [int(x) for x in topk.indices.tolist()]
            vals = [float(x) for x in topk.values.tolist()]
            refs: List[dict] = []
            for rel in rel_idxs:
                abs_idx = int(idxs[rel].item())
                if abs_idx < 0:
                    continue
                thumb = None
                if abs_idx < len(_gallery_paths):
                    gpath = _gallery_paths[abs_idx]
                    thumb = _thumb_b64(gpath, size=thumb_px)
                if thumb:
                    serial, crop_num = _parse_serial_crop_from_path(gpath)
                    refs.append({"img": thumb, "serial": serial, "crop": crop_num})
            out.append({
                "name": cat,
                "conf": vals[0] if vals else None,
                "refs": refs,
            })
        except Exception:
            out.append({"name": cat, "conf": None, "refs": []})

    def _score(item: dict) -> float:
        try:
            val = item.get("conf")
            if val is None:
                return -1e9
            return float(val)
        except Exception:
            return -1e9
    out.sort(key=_score, reverse=True)
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

def _sam_refine_box(img_array: Any, prompt_box: List[float]) -> Tuple[float, float, float, float]:
    """Use SAM to refine a bounding box based on mask fit.

    SAM2 returns multiple candidate masks. We select the one whose bounding
    box has the best IoU with the original prompt, giving the tightest fit.
    """
    import numpy as np
    _ensure_sam()
    if _sam is None:
        return tuple(prompt_box)
    try:
        results = _sam(img_array, bboxes=[prompt_box], verbose=False)
    except Exception:
        return tuple(prompt_box)
    if not results or not results[0].masks:
        return tuple(prompt_box)

    masks_data = results[0].masks.data.cpu().numpy()
    px1, py1, px2, py2 = prompt_box

    best_box = None
    best_iou = -1.0

    for idx in range(masks_data.shape[0]):
        mask = masks_data[idx].astype(bool)
        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        if not (np.any(rows) and np.any(cols)):
            continue
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        mx1, my1, mx2, my2 = float(cmin), float(rmin), float(cmax + 1), float(rmax + 1)

        #IoU between mask bbox and prompt bbox
        ix1 = max(mx1, px1)
        iy1 = max(my1, py1)
        ix2 = min(mx2, px2)
        iy2 = min(my2, py2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        area_m = (mx2 - mx1) * (my2 - my1)
        area_p = (px2 - px1) * (py2 - py1)
        union = area_m + area_p - inter
        iou = inter / union if union > 0 else 0.0

        if iou > best_iou:
            best_iou = iou
            best_box = (mx1, my1, mx2, my2)

    if best_box is not None:
        h, w = masks_data.shape[-2:]
        return (
            max(0.0, best_box[0]),
            max(0.0, best_box[1]),
            min(float(w), best_box[2]),
            min(float(h), best_box[3]),
        )
    return tuple(prompt_box)

def detect_with_sam(image_bytes: bytes) -> DetectWithSamResult:
    """Run YOLO detection then refine each box with SAM segmentation."""
    import numpy as np
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    img_array = np.array(img)
    
    dets = _run_yolo(img)
    refined_boxes = []
    
    for d in dets:
        prompt_box = list(d.xyxy)
        refined = _sam_refine_box(img_array, prompt_box)
        refined_boxes.append(refined)
    
    #Draw refined boxes
    refined_dets = [Det(xyxy=b, conf=1.0) for b in refined_boxes]
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
    """Refine provided YOLO-normalized boxes with SAM; returns absolute xyxy boxes."""
    import numpy as np
    img = _open_rgb_image(io.BytesIO(image_bytes))
    _enforce_max_dim(img)
    img_array = np.array(img)
    img_w, img_h = img.size
    refined: List[Tuple[float, float, float, float]] = []
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
        rx1, ry1, rx2, ry2 = x1, y1, x2, y2
        for _ in range(max(1, passes)):
            prompt = [rx1, ry1, rx2, ry2]
            nrx1, nry1, nrx2, nry2 = _sam_refine_box(img_array, prompt)
            rx1, ry1, rx2, ry2 = nrx1, nry1, nrx2, nry2
        #Clamp and normalize ordering
        rx1 = max(0.0, min(float(img_w), float(rx1)))
        ry1 = max(0.0, min(float(img_h), float(ry1)))
        rx2 = max(0.0, min(float(img_w), float(rx2)))
        ry2 = max(0.0, min(float(img_h), float(ry2)))
        if rx2 < rx1:
            rx1, rx2 = rx2, rx1
        if ry2 < ry1:
            ry1, ry2 = ry2, ry1
        refined.append((rx1, ry1, rx2, ry2))
    return refined


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
    global _gallery_emb, _gallery_names, _gallery_paths, _labeler_ref_cache, _labeler_ref_ready, _labeler_ref_task
    global _labeler_ref_progress_total, _labeler_ref_progress_built
    global _manual_ref_cache, _manual_ref_ready, _manual_ref_task, _manual_ref_progress_total, _manual_ref_progress_built, _manual_ref_per_cat
    global _thumb_cache, _resolved_gallery_path_cache, _gallery_crop_roots
    _ensure_device_only()
    try:
        if path:
            settings.cv_gallery_path = str(path)
        target = settings.cv_gallery_path
        try:
            gal_data = torch.load(target, map_location=_device, weights_only=False)
        except Exception:
            gal_data = torch.load(target, map_location=_device)

        emb = gal_data["emb"].to(_device)
        emb = torch.nn.functional.normalize(emb, p=2, dim=1)
        idx_to_class = {v: k for k, v in gal_data["class_to_idx"].items()}
        labels = gal_data["label"]
        names = [idx_to_class[int(i)] for i in labels]
        raw_paths = gal_data.get("path") or gal_data.get("paths") or gal_data.get("img_paths") or []
        paths: List[str]
        if isinstance(raw_paths, (list, tuple)) and len(raw_paths) == emb.shape[0]:
            paths = [str(p) for p in raw_paths]
        else:
            paths = []

        _gallery_emb = emb
        _gallery_names = names
        _gallery_paths = paths
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
        }
    except Exception as e:
        log_action("viz_gallery_refresh_error", "error", str(e))
        return {"ok": False, "error": str(e)}
