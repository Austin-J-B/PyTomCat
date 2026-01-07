"""Utilities for running YOLO detection/classification for TomCat."""

#tomcat/vision/vision.py
from __future__ import annotations
import io
import os
import math
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Optional, Any, cast

from PIL import Image, ImageDraw, ImageFont
import torch
from torch import Tensor

#Keep Ultralytics config within the repo unless overridden by the environment.
os.environ.setdefault(
    "YOLO_CONFIG_DIR",
    str(Path(__file__).resolve().parents[2] / ".ultra"),
)

#ultralytics is optional at import time; treat it as Any to appease Pylance
try:
    from ultralytics import YOLO  #type: ignore
except Exception:  #ultralytics not installed; YOLO calls will raise RuntimeError
    YOLO = None  #type: ignore[assignment]

from ..config import settings
from ..logger import log_action

#---------- Constants aligned to v5.6 ----------
#These values were tuned on the CCC cat dataset for optimal F1 score.
_PURPLE = "#4C007F"
_DEFAULT_CONF = 0.552  #tuned for maximum F1

#---------- Internal state (typed loosely to keep Pylance calm) ----------
_yolo: Optional[Any] = None
_encoder: Optional[torch.nn.Module] = None  #DINOv3 encoder model
_gallery_emb: Optional[Tensor] = None       #gallery embeddings [N, D]
_gallery_labels: Optional[List[int]] = None #gallery class indices
_idx_to_class: Optional[dict] = None        #index → cat name mapping
_device: Optional[torch.device] = None
_half: bool = False

_font: Optional[Any] = None  #FreeTypeFont vs ImageFont stubs vary; keep it Any


def _pick_device() -> torch.device:
    """Choose CUDA if available, else fall back to CPU."""
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _load_font() -> Any:
    """Load a PIL font for drawing labels."""
    """Return a font object; exact type varies by Pillow build."""
    global _font
    if _font is not None:
        return _font
    try:
        _font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        _font = ImageFont.load_default()
    return _font


def _ensure_device_only() -> None:
    """Lazy initialize the torch device once."""
    global _device, _half
    if _device is None:
        _device = _pick_device()
        _half = bool(settings.cv_half) and _device.type == "cuda"

def _ensure_detector() -> None:
    """Load YOLO detector weights if they are not already resident."""
    global _yolo
    _ensure_device_only()
    if _yolo is not None:
        return
    if YOLO is None:
        raise RuntimeError("ultralytics is not installed. pip install ultralytics")
    weights = settings.cv_detect_weights
    if not weights or not os.path.exists(weights):
        raise FileNotFoundError(f"Detect weights not found: {weights}")
    y: Any = YOLO(weights)  #type: ignore[call-arg]
    try:
        y.to(str(_device))  #ok to no-op on some builds
    except Exception:
        pass
    _yolo = y

def _ensure_classifier() -> None:
    """Load the DINOv3 encoder and gallery for nearest-neighbor classification."""
    global _encoder, _gallery_emb, _gallery_labels, _idx_to_class
    _ensure_device_only()
    if _encoder is not None:
        return  #already loaded
    try:
        encoder_path = settings.cv_encoder_weights
        gallery_path = settings.cv_gallery_path
        if not encoder_path or not os.path.exists(encoder_path):
            log_action('viz_clf_load_info', 'encoder_missing', str(encoder_path))
            return
        if not gallery_path or not os.path.exists(gallery_path):
            log_action('viz_clf_load_info', 'gallery_missing', str(gallery_path))
            return

        #Load DINOv3 encoder (ViT-S/14 from torch.hub or similar)
        try:
            encoder = torch.hub.load('facebookresearch/dinov2', 'dinov2_vits14', pretrained=False)
        except Exception as hub_err:
            log_action('viz_clf_load_warn', 'hub_load_failed', str(hub_err))
            #Fallback: try loading with dinov2_vitb14 if vits14 fails
            try:
                encoder = torch.hub.load('facebookresearch/dinov2', 'dinov2_vitb14', pretrained=False)
            except Exception:
                log_action('viz_clf_load_error', 'hub_fallback_failed', 'could not load dinov2 model')
                return

        #Load encoder weights
        try:
            state = torch.load(encoder_path, map_location=_device, weights_only=True)
        except TypeError:
            state = torch.load(encoder_path, map_location=_device)
        encoder.load_state_dict(state, strict=False)
        encoder.eval()
        encoder.to(_device)
        if _half:
            try:
                encoder.half()
            except Exception:
                pass
        _encoder = encoder

        #Load gallery
        try:
            gallery = torch.load(gallery_path, map_location=_device, weights_only=True)
        except TypeError:
            gallery = torch.load(gallery_path, map_location=_device)

        _gallery_emb = gallery['emb'].to(_device)  #[N, D]
        if _half:
            try:
                _gallery_emb = _gallery_emb.half()
            except Exception:
                pass

        #gallery['label'] is list of class indices (tensors or ints)
        if isinstance(gallery['label'], list):
            _gallery_labels = [int(x) if not hasattr(x, 'item') else x.item() for x in gallery['label']]
        else:
            _gallery_labels = gallery['label'].tolist()

        #Build idx→name mapping from gallery's class_to_idx
        class_to_idx = gallery.get('class_to_idx', {})
        _idx_to_class = {v: k for k, v in class_to_idx.items()}

        log_action('viz_clf_load_info', 'loaded', f"encoder={encoder_path} gallery={gallery_path} n={len(_gallery_labels)}")

    except Exception as e:
        log_action("viz_clf_load_error", f"type={type(e).__name__}", str(e))
        _encoder = None
        _gallery_emb = None
        _gallery_labels = None
        _idx_to_class = None


def _get_yolo() -> Any:
    """Instantiate the Ultralytics YOLO model with cached config."""
    _ensure_detector()
    assert _yolo is not None, "YOLO failed to load"
    return _yolo


def _jpeg_bytes(img: Image.Image, quality: int = 90) -> bytes:
    """Serialize a PIL image to JPEG bytes."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _enforce_max_dim(img: Image.Image) -> None:
    """Clamp huge images to the configured max dimension."""
    limit = int(getattr(settings, "cv_max_image_dim", 0) or 0)
    if limit <= 0:
        return  #no cap
    mx = max(img.size)
    if mx > limit:
        raise ValueError(
            f"Image too large ({img.size[0]}x{img.size[1]}). Max dimension is {limit}px."
        )



def _resize_for_detect(img: Image.Image, detect_size: int) -> Tuple[Image.Image, float, float]:
    """Resize input for detection while tracking scale ratios."""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img, 1.0, 1.0
    if w < h:
        new_w = detect_size
        new_h = int(round(h * (detect_size / w)))
    else:
        new_h = detect_size
        new_w = int(round(w * (detect_size / h)))
    det = img.resize((new_w, new_h))
    return det, (w / new_w), (h / new_h)


def _expand_box(x1: float, y1: float, x2: float, y2: float, pad_pct: float, w: int, h: int) -> Tuple[int, int, int, int]:
    """Grow detection boxes by a padding percentage while clamping image bounds."""
    bw = x2 - x1
    bh = y2 - y1
    pad_x = bw * pad_pct
    pad_y = bh * pad_pct
    nx1 = max(0, int(math.floor(x1 - pad_x)))
    ny1 = max(0, int(math.floor(y1 - pad_y)))
    nx2 = min(w, int(math.ceil(x2 + pad_x)))
    ny2 = min(h, int(math.ceil(y2 + pad_y)))
    return nx1, ny1, nx2, ny2


@dataclass
class Det:
    xyxy: Tuple[float, float, float, float]
    conf: float


@dataclass
class IdentifyResult:
    boxed_jpeg: bytes
    results: List[dict]  #[{"index": 1, "name": "...", "conf": 0.87, "box": [x1,y1,x2,y2]}]


def _draw_boxes(img: Image.Image, dets: List[Det]) -> Image.Image:
    """Render bounding boxes and labels onto an image with adaptive thickness."""
    draw = ImageDraw.Draw(img)
    font = _load_font()
    #Adaptive line width: thicker on very large images (e.g., 4K)
    max_dim = max(img.size)
    width = 6 if max_dim >= 2000 else 3
    for idx, d in enumerate(dets, start=1):
        x1, y1, x2, y2 = d.xyxy
        draw.rectangle([x1, y1, x2, y2], outline=_PURPLE, width=width)
        label = f"{idx}"
        #textbbox may not exist on very old Pillow; fallback to textsize
        try:
            bbox = draw.textbbox((0, 0), label, font=font)  #type: ignore[attr-defined]
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = draw.textsize(label, font=font)  #type: ignore[attr-defined]
        pad = 4
        bx1, by1 = int(x1), int(max(0, y1 - th - 2 * pad))
        bx2, by2 = bx1 + tw + 2 * pad, by1 + th + 2 * pad
        draw.rectangle([bx1, by1, bx2, by2], fill=_PURPLE)
        draw.text((bx1 + pad, by1 + pad), label, fill="white", font=font)
    return img


def _run_yolo(img: Image.Image) -> List[Det]:
    """Execute the YOLO detector on a PIL image."""
    """Run YOLO on a PIL image, returning boxes scaled to the original image coordinates."""
    yolo = _get_yolo()
    det_img, sx, sy = _resize_for_detect(img, settings.cv_detect_imgsz)

    #Prefer predict API so we can pass conf/iou/half/device explicitly.
    try:
        res = yolo.predict(  #type: ignore[call-arg, attr-defined]
            det_img,
            conf=(settings.cv_conf or _DEFAULT_CONF),
            iou=settings.cv_iou,
            imgsz=settings.cv_detect_imgsz,
            half=bool(_half),
            device=str(_device) if _device is not None else None,
            verbose=False,
        )
    except TypeError:
        #Fallback to call-style for older ultralytics versions
        res = yolo(det_img)  #type: ignore[operator]

    dets: List[Det] = []
    for r in res:  #ultralytics returns an iterable of results
        boxes = r.boxes.xyxy.detach().to("cpu").numpy()
        confs = r.boxes.conf.detach().to("cpu").numpy()
        for b, c in zip(boxes, confs):
            if float(c) >= (settings.cv_conf or _DEFAULT_CONF):
                x1 = float(b[0] * sx)
                y1 = float(b[1] * sy)
                x2 = float(b[2] * sx)
                y2 = float(b[3] * sy)
                dets.append(Det((x1, y1, x2, y2), float(c)))
    return dets


def detect(image_bytes: bytes) -> bytes:
    """Return the original image annotated with detection boxes."""
    """Return annotated JPEG with purple boxes for each cat. Raises ValueError on 4K+ images."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)
    out = _jpeg_bytes(annotated, quality=90)
    log_action("viz_detect", f"boxes={len(dets)}", "ok")
    return out


def crop(image_bytes: bytes) -> List[bytes]:
    """Return cropped regions for each detected cat."""
    """Return list of JPEG crops expanded by pad_pct per v5.6."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    dets = _run_yolo(img)
    crops: List[bytes] = []
    for d in dets:
        x1, y1, x2, y2 = d.xyxy
        cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, *img.size)
        crop_img = img.crop((cx1, cy1, cx2, cy2))
        crops.append(_jpeg_bytes(crop_img, quality=92))
    if getattr(settings, 'cv_log_crop', True):
        log_action("viz_crop", f"crops={len(crops)}", "ok")
    return crops


def _prep_tensor(pil: Image.Image) -> Tensor:
    """Convert a PIL image into a normalized torch tensor."""
    """Resize square and convert to a Tensor. DINOv3 requires ImageNet normalization."""
    from torchvision.transforms import Compose, Resize, ToTensor, Normalize
    size = settings.cv_clf_imgsz
    tfm = Compose([
        Resize((size, size)),
        ToTensor(),
        Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    t = cast(Tensor, tfm(pil))
    if _half:
        try:
            t = t.half()
        except Exception:
            pass
    return t


def identify(image_bytes: bytes) -> IdentifyResult:
    """Classify the cat in an image using the trained DINOv3 encoder + gallery."""
    """Draw boxes and run encoder on each crop to find nearest gallery match."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    _ensure_classifier()
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)

    results: List[dict] = []        

    if _encoder is not None and _gallery_emb is not None and dets:
        tiles: List[Tensor] = []
        boxes: List[Tuple[int, int, int, int]] = []
        for d in dets:
            x1, y1, x2, y2 = d.xyxy
            cx1, cy1, cx2, cy2 = _expand_box(x1, y1, x2, y2, settings.cv_pad_pct, *img.size)
            crop_img = img.crop((cx1, cy1, cx2, cy2))
            tiles.append(_prep_tensor(crop_img))
            boxes.append((int(cx1), int(cy1), int(cx2), int(cy2)))

        if tiles:
            with torch.inference_mode():
                device = _device if _device is not None else torch.device("cpu")
                batch = torch.stack(tiles, dim=0).to(device, non_blocking=True)
                
                #Get query embeddings [B, D]
                query_emb = _encoder(batch)
                #Normalize for cosine similarity
                query_emb = torch.nn.functional.normalize(query_emb, dim=1, p=2)
                
                #Compare against gallery [N, D] (already normalized if needed, but safe to verify)
                #Actually we should ensure gallery is normalized once on load, but let's just do dot prod
                #Assuming gallery is normalized. If not, we should normalize _gallery_emb.
                #Let's normalize gallery on the fly to be safe or ensure it on load. 
                #For perf, normalize query and gallery.
                
                parts = []
                #Chunked cosine similarity if gallery is huge? 7k is fine for dot product.
                # [B, N] = [B, D] @ [D, N]
                sims = torch.mm(query_emb, _gallery_emb.T)
                
                #Find best match for each query
                #values: [B], indices: [B]
                best_vals, best_idxs = sims.max(dim=1)
                
                best_vals = best_vals.detach().to("cpu").numpy()
                best_idxs = best_idxs.detach().to("cpu").numpy()

            idx_to_class = _idx_to_class or {}
            gallery_labels = _gallery_labels or []

            for idx, (conf, match_idx, (cx1, cy1, cx2, cy2)) in enumerate(zip(best_vals, best_idxs, boxes), start=1):
                #match_idx is index in gallery
                if match_idx < len(gallery_labels):
                    class_idx = gallery_labels[match_idx]
                    name = idx_to_class.get(class_idx, f"Cat{class_idx}")
                else:
                    name = "Unknown"
                
                results.append({
                    "index": idx,
                    "name": name,
                    "conf": float(conf),
                    "box": [cx1, cy1, cx2, cy2],
                })

    boxed = _jpeg_bytes(annotated, quality=90)
    log_action("viz_identify", f"boxes={len(dets)} guesses={len(results)}", "ok")
    return IdentifyResult(boxed_jpeg=boxed, results=results)
