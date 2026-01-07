"""Utilities for running YOLO detection and DINOv3 ReID similarity for TomCat."""

from __future__ import annotations
import io
import os
import math
import warnings
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

try:
    from ultralytics import YOLO
except Exception:
    YOLO = None

from ..config import settings
from ..logger import log_action

#---------- Constants ----------
_PURPLE = "#4C007F"
_DEFAULT_CONF = 0.552

#---------- Internal State ----------
_yolo: Optional[Any] = None
_clf: Optional[torch.nn.Module] = None
_gallery_emb: Optional[Tensor] = None
_gallery_names: List[str] = []
_device: Optional[torch.device] = None
_half: bool = False
_font: Optional[Any] = None

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

def _ensure_classifier() -> None:
    """Load the DINOv3 encoder and the .pt gallery."""
    global _clf, _gallery_emb, _gallery_names
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