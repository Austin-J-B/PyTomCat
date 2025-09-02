# tomcat/vision/vision.py
from __future__ import annotations
import io
import os
import math
from dataclasses import dataclass
from typing import List, Tuple, Optional, Any, cast

from PIL import Image, ImageDraw, ImageFont
import torch
from torch import Tensor

# ultralytics is optional at import time; treat it as Any to appease Pylance
try:
    from ultralytics import YOLO  # type: ignore
except Exception:
    YOLO = None  # type: ignore[assignment]

from ..config import settings
from ..logger import log_action

# ---------- Constants aligned to v5.6 ----------
_PURPLE = "#4C007F"
_DEFAULT_CONF = 0.552  # your max-F1

# ---------- Internal state (typed loosely to keep Pylance calm) ----------
_yolo: Optional[Any] = None
_clf: Optional[torch.nn.Module] = None
_device: Optional[torch.device] = None
_half: bool = False

_font: Optional[Any] = None  # FreeTypeFont vs ImageFont stubs vary; keep it Any


def _pick_device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _load_font() -> Any:
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
    global _device, _half
    if _device is None:
        _device = _pick_device()
        _half = bool(settings.cv_half) and _device.type == "cuda"

def _ensure_detector() -> None:
    global _yolo
    _ensure_device_only()
    if _yolo is not None:
        return
    if YOLO is None:
        raise RuntimeError("ultralytics is not installed. pip install ultralytics")
    weights = settings.cv_detect_weights
    if not weights or not os.path.exists(weights):
        raise FileNotFoundError(f"Detect weights not found: {weights}")
    y: Any = YOLO(weights)  # type: ignore[call-arg]
    try:
        y.to(str(_device))  # ok to no-op on some builds
    except Exception:
        pass
    _yolo = y

def _ensure_classifier() -> None:
    """Load classifier lazily; never crash detector if classifier is bad."""
    global _clf
    _ensure_device_only()
    if _clf is not None:
        return
    try:
        ckpt_path = settings.cv_classify_weights
        if not ckpt_path or not os.path.exists(ckpt_path):
            return  # classifier is optional
        try:
            state = torch.load(ckpt_path, map_location=_device, weights_only=True)  # torch>=2.4
        except TypeError:
            # Older torch without weights_only
            state = torch.load(ckpt_path, map_location=_device)

        # Infer num_classes from checkpoint if possible
        sd = state.get("state_dict", state) if isinstance(state, dict) else state
        fc_w = None
        if isinstance(sd, dict):
            for k, v in sd.items():
                if k.endswith("fc.weight") and hasattr(v, "shape"):
                    fc_w = v
                    break
        if fc_w is not None and hasattr(fc_w, "shape"):
            num_classes = int(fc_w.shape[0])
        else:
            # fallback to config length or 1
            num_classes = max(1, len(settings.cv_class_names) or 1)

        # Build model head with the right class count
        from torchvision.models import resnet18
        from torch import nn

        model = resnet18(weights=None)
        model.fc = nn.Linear(model.fc.in_features, num_classes)
        model.load_state_dict(sd if isinstance(sd, dict) else state, strict=False)
        model.eval()
        model.to(_device)
        if _half:
            try:
                model.half()
            except Exception:
                pass

        # Ensure class names length matches; fill with Cat{i}
        if len(settings.cv_class_names) < num_classes:
            settings.cv_class_names.extend(
                [f"Cat{i}" for i in range(len(settings.cv_class_names), num_classes)]
            )

        _clf = model
    except Exception as e:
        # Do NOT let a bad classifier kill detect/crop. Just log and continue.
        log_action("viz_clf_load_error", f"type={type(e).__name__}", str(e))
        _clf = None


def _get_yolo() -> Any:
    _ensure_detector()
    assert _yolo is not None, "YOLO failed to load"
    return _yolo


def _jpeg_bytes(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _enforce_max_dim(img: Image.Image) -> None:
    limit = int(getattr(settings, "cv_max_image_dim", 0) or 0)
    if limit <= 0:
        return  # no cap
    mx = max(img.size)
    if mx > limit:
        raise ValueError(
            f"Image too large ({img.size[0]}x{img.size[1]}). Max dimension is {limit}px."
        )



def _resize_for_detect(img: Image.Image, detect_size: int) -> Tuple[Image.Image, float, float]:
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
    results: List[dict]  # [{"index": 1, "name": "...", "conf": 0.87, "box": [x1,y1,x2,y2]}]


def _draw_boxes(img: Image.Image, dets: List[Det]) -> Image.Image:
    draw = ImageDraw.Draw(img)
    font = _load_font()
    for idx, d in enumerate(dets, start=1):
        x1, y1, x2, y2 = d.xyxy
        draw.rectangle([x1, y1, x2, y2], outline=_PURPLE, width=3)
        label = f"{idx}"
        # textbbox may not exist on very old Pillow; fallback to textsize
        try:
            bbox = draw.textbbox((0, 0), label, font=font)  # type: ignore[attr-defined]
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = draw.textsize(label, font=font)  # type: ignore[attr-defined]
        pad = 4
        bx1, by1 = int(x1), int(max(0, y1 - th - 2 * pad))
        bx2, by2 = bx1 + tw + 2 * pad, by1 + th + 2 * pad
        draw.rectangle([bx1, by1, bx2, by2], fill=_PURPLE)
        draw.text((bx1 + pad, by1 + pad), label, fill="white", font=font)
    return img


def _run_yolo(img: Image.Image) -> List[Det]:
    """Run YOLO on a PIL image, returning boxes scaled to the original image coordinates."""
    yolo = _get_yolo()
    det_img, sx, sy = _resize_for_detect(img, settings.cv_detect_imgsz)

    # Prefer predict API so we can pass conf/iou/half/device explicitly.
    try:
        res = yolo.predict(  # type: ignore[call-arg, attr-defined]
            det_img,
            conf=(settings.cv_conf or _DEFAULT_CONF),
            iou=settings.cv_iou,
            imgsz=settings.cv_detect_imgsz,
            half=bool(_half),
            device=str(_device) if _device is not None else None,
            verbose=False,
        )
    except TypeError:
        # Fallback to call-style for older ultralytics versions
        res = yolo(det_img)  # type: ignore[operator]

    dets: List[Det] = []
    for r in res:  # ultralytics returns an iterable of results
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
    """Return annotated JPEG with purple boxes for each cat. Raises ValueError on 4K+ images."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)
    out = _jpeg_bytes(annotated, quality=90)
    log_action("viz_detect", f"boxes={len(dets)}", "ok")
    return out


def crop(image_bytes: bytes) -> List[bytes]:
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
    log_action("viz_crop", f"crops={len(crops)}", "ok")
    return crops


def _prep_tensor(pil: Image.Image) -> Tensor:
    """Resize square and convert to a Tensor. v5.6 parity: no ImageNet normalization."""
    from torchvision.transforms import Compose, Resize, ToTensor  # local import to avoid global hard deps
    size = settings.cv_clf_imgsz
    tfm = Compose([Resize((size, size)), ToTensor()])
    t = cast(Tensor, tfm(pil))
    if _half:
        try:
            t = t.half()
        except Exception:
            pass
    return t


def identify(image_bytes: bytes) -> IdentifyResult:
    """Draw boxes and run classifier on each crop. Returns boxed JPEG + per-box guesses."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    _enforce_max_dim(img)
    _ensure_classifier()
    dets = _run_yolo(img)
    annotated = _draw_boxes(img.copy(), dets)

    results: List[dict] = []        

    if _clf is not None and dets:
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
                logits = _clf(batch)  # type: ignore[operator]
                probs = torch.softmax(logits, dim=1).detach().to("cpu").numpy()

            names = settings.cv_class_names or []
            for idx, (pvec, (cx1, cy1, cx2, cy2)) in enumerate(zip(probs, boxes), start=1):
                j = int(pvec.argmax())
                conf = float(pvec[j])
                guess = names[j] if j < len(names) else f"Cat{j}"
                results.append({
                    "index": idx,
                    "name": guess,
                    "conf": conf,
                    "box": [cx1, cy1, cx2, cy2],
                })

    boxed = _jpeg_bytes(annotated, quality=90)
    log_action("viz_identify", f"boxes={len(dets)} guesses={len(results)}", "ok")
    return IdentifyResult(boxed_jpeg=boxed, results=results)

