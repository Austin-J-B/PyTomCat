"""Utilities for running YOLO detection and DINOv3 ReID similarity for TomCat."""

from __future__ import annotations
import io
import os
import math
import warnings
import base64
import asyncio
import random
import re
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Optional, Any, cast

from PIL import Image, ImageDraw, ImageFont
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
_clf: Optional[torch.nn.Module] = None
_gallery_emb: Optional[Tensor] = None
_gallery_names: List[str] = []
_gallery_paths: List[str] = []
_gallery_root_hints: Optional[List[Path]] = None
_device: Optional[torch.device] = None
_half: bool = False
_font: Optional[Any] = None
_labeler_ref_cache: dict[str, dict[str, Any]] = {}
_labeler_ref_ready: bool = False
_labeler_ref_building: bool = False
_labeler_ref_task: Optional[asyncio.Task] = None

#Sheet column indices (0-based) for TCB Pics Formatted
COL_URL = 6
COL_SERIAL = 7
COL_BOX_COORDS = 8
COL_BOX_CAT_IDS = 9
SN_PATTERN = re.compile(r"sn(\d+)", re.IGNORECASE)

def _parse_serial(val: str) -> Optional[int]:
    m = SN_PATTERN.search(val or "")
    if m:
        return int(m.group(1))
    if str(val or "").strip().isdigit():
        return int(str(val).strip())
    return None

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
    if _sam is not None: return
    if SAM is None: raise RuntimeError("ultralytics SAM not available")
    _sam = SAM(settings.cv_sam_weights)
    log_action("viz_sam_load", "sam_ready", settings.cv_sam_weights)

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

def _resolve_gallery_path(path: str) -> str:
    """Map gallery paths saved in training to local filesystem paths."""
    if not path:
        return path
    try:
        if os.path.exists(path):
            return path
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
                return str(candidate)
    return path

def _thumb_b64(path: str, size: int = 96) -> Optional[str]:
    """Load an image, generate a small JPEG thumbnail, and return base64."""
    try:
        resolved = _resolve_gallery_path(path)
        img = Image.open(resolved).convert("RGB")
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
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
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)
    
    buf = io.BytesIO()
    annotated.save(buf, format="JPEG")
    results = [{"box": d.xyxy, "conf": d.conf} for d in dets]
    return IdentifyResult(boxed_jpeg=buf.getvalue(), results=results)

def crop(image_bytes: bytes) -> IdentifyResult:
    """Run detection and return a collage of the cropped cats."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
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
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    
    _ensure_classifier()
    
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)
    results = []

    if _clf is not None and _gallery_emb is not None and dets:
        tiles, boxes = [], []
        for d in dets:
            x1, y1, x2, y2 = d.xyxy
            cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, *img.size)
            tiles.append(_prep_tensor(img.crop((cx1, cy1, cx2, cy2))))
            boxes.append((int(cx1), int(cy1), int(cx2), int(cy2)))

        if tiles:
            batch = torch.stack(tiles).to(_device)
            with torch.inference_mode():
                query_embs = _clf(batch)
                similarities = query_embs @ _gallery_emb.T
                
                # Sort all matches descending (best matches first)
                vals, idxs = torch.sort(similarities, dim=1, descending=True)
                
                for i in range(len(dets)):
                    top_candidates = []
                    seen_cats = set()
                    
                    # Iterate until we find 5 unique identities
                    for j in range(len(_gallery_names)):
                        if len(top_candidates) >= 5:
                            break
                            
                        cat_idx = int(idxs[i, j])
                        cat_conf = float(vals[i, j])
                        cat_name = _gallery_names[cat_idx]
                        
                        if cat_name not in seen_cats:
                            top_candidates.append((cat_name, cat_conf))
                            seen_cats.add(cat_name)
                    
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

def identify_boxes(
    image_bytes: bytes,
    boxes: List[Tuple[float, float, float, float]],
    *,
    top_k: int = 9,
    refs_per: int = 5,
    thumb_size: int = 128,
) -> IdentifyResult:
    """Run DINOv3 identification on specific normalized boxes (cx, cy, w, h)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    _ensure_classifier()

    if _clf is None or _gallery_emb is None or not boxes:
        return IdentifyResult(boxed_jpeg=b"", results=[])

    img_w, img_h = img.size
    tiles: List[Tensor] = []
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
            if len(candidate_names) >= top_k:
                break

        ref_lists: dict[str, List[dict]] = {n: [] for n in candidate_names}
        if _labeler_ref_ready:
            for name in candidate_names:
                ref_lists[name] = _get_labeler_refs_for_cat(name, query_embs[i], refs_per)
        elif _gallery_paths:
            done = 0
            target_total = refs_per * len(candidate_names)
            for j in range(len(_gallery_names)):
                if done >= target_total:
                    break
                cat_idx = int(idxs[j])
                cat_name = _gallery_names[cat_idx]
                if cat_name not in ref_lists:
                    continue
                if len(ref_lists[cat_name]) >= refs_per:
                    continue
                if cat_idx < len(_gallery_paths):
                    thumb = _thumb_b64(_gallery_paths[cat_idx], size=thumb_size)
                    if thumb:
                        ref_lists[cat_name].append({"img": thumb, "serial": None, "crop": None})
                        done += 1

        candidates = []
        for name, conf in zip(candidate_names, candidate_scores):
            candidates.append({
                "name": name,
                "conf": conf,
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
    batch_size = 32
    out = []
    for i in range(0, len(tensors), batch_size):
        batch = torch.stack(tensors[i:i + batch_size]).to(_device)
        with torch.inference_mode():
            emb = _clf(batch)
        out.append(emb.detach().cpu())
    return torch.cat(out, dim=0) if out else torch.empty((0, 512))

async def warm_labeler_refs(force: bool = False) -> dict:
    """Warm per-cat reference cache from TCB Pics Formatted rows."""
    global _labeler_ref_ready, _labeler_ref_building, _labeler_ref_task, _labeler_ref_cache
    if _labeler_ref_building:
        return {"ready": _labeler_ref_ready, "building": True, "cats": len(_labeler_ref_cache)}
    if _labeler_ref_ready and not force:
        return {"ready": True, "building": False, "cats": len(_labeler_ref_cache)}

    async def _build() -> None:
        global _labeler_ref_ready, _labeler_ref_building, _labeler_ref_cache
        _labeler_ref_building = True
        try:
            await asyncio.to_thread(_ensure_classifier)
            cat_list = await asyncio.to_thread(get_all_cats)
            cat_map = {c.lower(): c for c in cat_list}
            max_per_cat = int(getattr(settings, "labeler_ref_per_cat", 50) or 50)
            thumb_size = int(getattr(settings, "labeler_ref_thumb_size", 96) or 96)

            from ..services.catsheets import get_tcb_pics_rows
            from ..services import labeler_cache

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
                if not url.startswith("http"):
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
            for cat, entries in samples.items():
                if not entries:
                    continue
                crops: List[Image.Image] = []
                refs: List[dict] = []
                for sn, url, coord_str, crop_idx in entries:
                    coord = _parse_yolo_box_str(coord_str)
                    if coord is None:
                        continue
                    data = await labeler_cache.get_or_download(sn, url)
                    if not data:
                        continue
                    try:
                        img = Image.open(io.BytesIO(data)).convert("RGB")
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
                if not crops:
                    continue
                emb = await asyncio.to_thread(_embed_crops, crops)
                if emb.numel() == 0:
                    continue
                new_cache[cat] = {"emb": emb, "refs": refs}

            _labeler_ref_cache = new_cache
            _labeler_ref_ready = True
        except Exception as e:
            log_action("labeler_ref_build_error", "error", str(e))
            _labeler_ref_ready = False
        finally:
            _labeler_ref_building = False

    _labeler_ref_task = asyncio.create_task(_build())
    return {"ready": _labeler_ref_ready, "building": True, "cats": len(_labeler_ref_cache)}

def labeler_ref_status() -> dict:
    return {
        "ready": _labeler_ref_ready,
        "building": _labeler_ref_building,
        "cats": len(_labeler_ref_cache),
    }

def _get_labeler_refs_for_cat(cat: str, query_emb: Tensor, refs_per: int) -> List[str]:
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
    """Use SAM to refine a bounding box based on mask fit."""
    import numpy as np
    _ensure_sam()
    results = _sam(img_array, bboxes=[prompt_box], verbose=False)
    if results and results[0].masks:
        mask = results[0].masks.data[0].cpu().numpy().astype(bool)
        h, w = mask.shape[-2:]
        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        if np.any(rows) and np.any(cols):
            rmin, rmax = np.where(rows)[0][[0, -1]]
            cmin, cmax = np.where(cols)[0][[0, -1]]
            #Add small margin
            return (
                max(0, float(cmin - 5)),
                max(0, float(rmin - 5)),
                min(w, float(cmax + 5)),
                min(h, float(rmax + 5))
            )
    return tuple(prompt_box)

def detect_with_sam(image_bytes: bytes) -> DetectWithSamResult:
    """Run YOLO detection then refine each box with SAM segmentation."""
    import numpy as np
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
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

def get_all_cats() -> List[str]:
    """Return sorted list of all unique cat names from the gallery."""
    _ensure_classifier()
    if not _gallery_names:
        return []
    return sorted(set(_gallery_names))
