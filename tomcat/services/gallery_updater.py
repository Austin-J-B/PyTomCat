"""Build and publish a refreshed DINOv3 gallery from labeled sheet crops."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from PIL import Image
import torch
from torch import Tensor

from ..config import settings
from ..logger import log_action
from ..services.catsheets import force_refresh_tcb_cache
from ..services.sheets_client import sheets_client
from ..services import labeler_cache
from ..vision.vision import DINOv3Wrapper, refresh_gallery


# TCB Pics Formatted columns (0-based).
COL_URL = 6
COL_SERIAL = 7
COL_BOX_COORDS = 8
COL_BOX_CAT_IDS = 9

SN_PATTERN = re.compile(r"sn(\d+)", re.IGNORECASE)
_REJECTED = {"rejected", "needsreview", "needs review", "0. notacat", "notacat"}
_DEFAULT_MIN_PIXELS = int(os.getenv("GALLERY_MIN_PIXELS", "122500") or "122500")
_DEFAULT_MIN_PER_CAT = int(os.getenv("GALLERY_MIN_PER_CAT", "4") or "4")
_DEFAULT_BATCH_SIZE = int(os.getenv("GALLERY_EMBED_BATCH_SIZE", "32") or "32")
_DEFAULT_TIMEOUT_SEC = float(os.getenv("GALLERY_DOWNLOAD_TIMEOUT_SEC", "20") or "20")
_DEFAULT_TTA_HFLIP = str(os.getenv("GALLERY_TTA_HFLIP", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _parse_serial(val: str) -> Optional[int]:
    sval = str(val or "").strip()
    m = SN_PATTERN.search(sval)
    if m:
        return int(m.group(1))
    if sval.isdigit():
        return int(sval)
    return None


def _normalize_label(raw: str) -> Optional[str]:
    label = str(raw or "").strip()
    if not label:
        return None
    low = label.lower()
    if low in _REJECTED:
        return None
    m = re.match(r"^\s*\d+\.\s*(.+)$", label)
    if m:
        label = m.group(1).strip()
    if not label:
        return None
    if label.lower() in _REJECTED:
        return None
    return label


def _parse_box(box_str: str) -> Optional[Tuple[float, float, float, float]]:
    try:
        parts = [float(p) for p in str(box_str or "").strip().split()]
    except Exception:
        return None
    if len(parts) != 4:
        return None
    cx, cy, w, h = parts
    if w <= 0 or h <= 0:
        return None
    return cx, cy, w, h


def _expand_box(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    pad_pct: float,
    img_w: int,
    img_h: int,
) -> Tuple[int, int, int, int]:
    w = x2 - x1
    h = y2 - y1
    pw = w * pad_pct
    ph = h * pad_pct
    ex1 = max(0.0, x1 - pw)
    ey1 = max(0.0, y1 - ph)
    ex2 = min(float(img_w), x2 + pw)
    ey2 = min(float(img_h), y2 + ph)
    return int(ex1), int(ey1), int(ex2), int(ey2)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s or "cat"


def _extract_drive_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if "drive.google.com" not in parsed.netloc and "googleusercontent.com" not in parsed.netloc:
        return None
    qs = parse_qs(parsed.query or "")
    if qs.get("id"):
        return qs["id"][0]
    parts = [p for p in parsed.path.split("/") if p]
    if "d" in parts:
        idx = parts.index("d")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _looks_like_image(data: bytes, content_type: str | None = None) -> bool:
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct.startswith("image/"):
            return True
    if not data:
        return False
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _download_image(url: str, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> Optional[bytes]:
    try:
        candidates = [url]
        drive_id = _extract_drive_id(url)
        if drive_id:
            candidates.extend([
                f"https://drive.google.com/uc?export=download&id={drive_id}",
                f"https://drive.google.com/uc?export=view&id={drive_id}",
                f"https://drive.usercontent.google.com/download?id={drive_id}&export=download",
            ])
        seen: set[str] = set()
        for cand in candidates:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            req = Request(cand, headers={"User-Agent": "TomCatGalleryUpdater/1.0"})
            with urlopen(req, timeout=timeout_sec) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if _looks_like_image(data, ctype):
                    return data
        return None
    except Exception:
        return None


def _get_image_bytes(serial: int, url: str) -> Optional[bytes]:
    try:
        cached = labeler_cache.get_cached_image(int(serial))
        if cached:
            return cached
    except Exception:
        pass

    data = _download_image(url)
    if not data:
        return None

    try:
        cache_dir = Path("cache") / "labeler"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"sn{int(serial):04d}.jpg").write_bytes(data)
    except Exception:
        pass
    return data


def _next_version_path(weights_dir: Path) -> Path:
    pat = re.compile(r"^R4\.5\.(\d+)_cat_DINOv3_gallery\.pt$", re.IGNORECASE)
    max_n = 0
    if weights_dir.exists():
        for p in weights_dir.iterdir():
            m = pat.match(p.name)
            if m:
                try:
                    max_n = max(max_n, int(m.group(1)))
                except Exception:
                    pass
    return weights_dir / f"R4.5.{max_n + 1}_cat_DINOv3_gallery.pt"


def _load_rows() -> List[List[str]]:
    gc = sheets_client()
    sh = gc.open_by_key(settings.sheet_catabase_id)
    ws = sh.worksheet("TCB Pics Formatted")
    return ws.get_all_values()


def _load_encoder(device: torch.device) -> torch.nn.Module:
    model = DINOv3Wrapper()
    try:
        state = torch.load(settings.cv_encoder_weights, map_location=device, weights_only=True)
    except Exception:
        state = torch.load(settings.cv_encoder_weights, map_location=device)
    model.load_state_dict(state, strict=True)
    model.to(device).eval()
    return model


def _prep_tensor(img: Image.Image) -> Tensor:
    from torchvision.transforms import Compose, Normalize, Resize, ToTensor

    tfm = Compose([
        Resize((int(settings.cv_clf_imgsz), int(settings.cv_clf_imgsz))),
        ToTensor(),
        Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    return tfm(img)


def run_gallery_update(
    *,
    mode: str = "full",
    min_pixels: int = _DEFAULT_MIN_PIXELS,
    min_per_cat: int = _DEFAULT_MIN_PER_CAT,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    tta_hflip: bool = _DEFAULT_TTA_HFLIP,
) -> Dict[str, Any]:
    """Rebuild the gallery from sheet labels and publish it as the active gallery.

    Mode is currently coerced to `full` to ensure label corrections are reflected.
    """
    started_at = datetime.now().isoformat()
    mode_req = str(mode or "full").strip().lower()
    mode_eff = "full"

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = Path("cache") / "gallery_retrain" / "work" / run_id
    crop_root = work_dir / "crops"
    work_dir.mkdir(parents=True, exist_ok=True)

    stats: Dict[str, Any] = {
        "mode_requested": mode_req,
        "mode_effective": mode_eff,
        "tta_hflip": bool(tta_hflip),
        "rows": 0,
        "rows_with_boxes": 0,
        "crop_candidates": 0,
        "crop_saved": 0,
        "crop_filtered_small": 0,
        "cats_before_filter": 0,
        "cats_after_filter": 0,
    }

    cat_to_crops: Dict[str, List[Tuple[str, Path]]] = defaultdict(list)
    unique_crops: set[str] = set()

    try:
        rows = _load_rows()
        stats["rows"] = max(0, len(rows) - 1)
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            serial = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if serial is None:
                continue
            url = (row[COL_URL] if len(row) > COL_URL else "").strip()
            if not url.startswith("http"):
                continue
            box_coords = (row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "").strip()
            box_labels = (row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "").strip()
            if not box_coords or not box_labels:
                continue
            if box_coords.lower() == "rejected":
                continue

            coords = [c.strip() for c in box_coords.split("|") if c.strip()]
            labels = [c.strip() for c in box_labels.split("|") if c.strip()]
            if not coords or not labels:
                continue

            stats["rows_with_boxes"] += 1
            limit = min(len(coords), len(labels))
            image_bytes = _get_image_bytes(int(serial), url)
            if not image_bytes:
                continue
            try:
                image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            except Exception:
                continue
            iw, ih = image.size

            for idx in range(limit):
                stats["crop_candidates"] += 1
                cat_name = _normalize_label(labels[idx])
                if not cat_name:
                    continue
                box = _parse_box(coords[idx])
                if box is None:
                    continue
                cx, cy, w, h = box
                x1 = (cx - w / 2) * iw
                y1 = (cy - h / 2) * ih
                x2 = (cx + w / 2) * iw
                y2 = (cy + h / 2) * ih
                ex1, ey1, ex2, ey2 = _expand_box(x1, y1, x2, y2, float(settings.cv_pad_pct), iw, ih)
                if ex2 <= ex1 or ey2 <= ey1:
                    continue
                area = (ex2 - ex1) * (ey2 - ey1)
                if area < int(min_pixels):
                    stats["crop_filtered_small"] += 1
                    continue
                crop = image.crop((ex1, ey1, ex2, ey2))
                crop_id = f"sn{int(serial):04d}_c{idx + 1:02d}"
                unique_key = f"{cat_name.casefold()}::{crop_id}"
                if unique_key in unique_crops:
                    continue
                unique_crops.add(unique_key)
                cat_slug = _slug(cat_name)
                cat_dir = crop_root / cat_slug
                cat_dir.mkdir(parents=True, exist_ok=True)
                crop_path = cat_dir / f"{crop_id}.jpg"
                try:
                    crop.save(crop_path, format="JPEG", quality=95)
                except Exception:
                    continue
                cat_to_crops[cat_name].append((crop_id, crop_path))
                stats["crop_saved"] += 1

        stats["cats_before_filter"] = len(cat_to_crops)
        eligible_cats = sorted([c for c, items in cat_to_crops.items() if len(items) >= int(min_per_cat)])
        stats["cats_after_filter"] = len(eligible_cats)
        if not eligible_cats:
            raise RuntimeError("No eligible cats after quality filtering")

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = _load_encoder(device)

        class_to_idx = {cat: i for i, cat in enumerate(eligible_cats)}
        all_emb: List[Tensor] = []
        all_labels: List[int] = []
        all_paths: List[str] = []

        for cat in eligible_cats:
            items = cat_to_crops[cat]
            cat_idx = class_to_idx[cat]
            for start in range(0, len(items), int(max(1, batch_size))):
                batch = items[start:start + int(max(1, batch_size))]
                tensors: List[Tensor] = []
                ids: List[str] = []
                for crop_id, crop_path in batch:
                    try:
                        img = Image.open(crop_path).convert("RGB")
                    except Exception:
                        continue
                    tensors.append(_prep_tensor(img))
                    ids.append(crop_id)
                if not tensors:
                    continue
                batch_t = torch.stack(tensors).to(device)
                with torch.inference_mode():
                    emb = model(batch_t)
                    if tta_hflip:
                        emb_flip = model(torch.flip(batch_t, dims=[3]))
                        emb = torch.nn.functional.normalize((emb + emb_flip) / 2.0, p=2, dim=1)
                    emb = emb.detach().cpu()
                all_emb.append(emb)
                all_labels.extend([cat_idx] * emb.shape[0])
                for crop_id in ids:
                    all_paths.append(f"sheet://{crop_id}:{cat}")

        if not all_emb or not all_labels:
            raise RuntimeError("No embeddings generated")

        emb_tensor = torch.cat(all_emb, dim=0)
        emb_tensor = torch.nn.functional.normalize(emb_tensor, p=2, dim=1)
        label_tensor = torch.tensor(all_labels, dtype=torch.long)
        gallery_obj = {
            "emb": emb_tensor,
            "label": label_tensor,
            "class_to_idx": class_to_idx,
            "path": all_paths,
        }

        active_gallery_path = Path(settings.cv_gallery_path)
        weights_dir = active_gallery_path.parent if active_gallery_path.parent else Path("weights")
        weights_dir.mkdir(parents=True, exist_ok=True)
        version_path = _next_version_path(weights_dir)

        torch.save(gallery_obj, version_path)
        if version_path.resolve() != active_gallery_path.resolve():
            active_gallery_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(gallery_obj, active_gallery_path)

        refresh_state = refresh_gallery(str(active_gallery_path))
        force_refresh_tcb_cache()

        result = {
            "status": "ok",
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
            "active_gallery_path": str(active_gallery_path),
            "versioned_gallery_path": str(version_path),
            "embeddings": int(emb_tensor.shape[0]),
            "cats": int(len(class_to_idx)),
            "reload": refresh_state,
            "stats": stats,
        }
        try:
            out_meta = Path("cache") / "gallery_retrain" / "last_run.json"
            out_meta.parent.mkdir(parents=True, exist_ok=True)
            out_meta.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        log_action(
            "gallery_updater",
            f"cats={result['cats']} embeddings={result['embeddings']}",
            f"active={active_gallery_path.name} tta_hflip={int(bool(tta_hflip))}",
        )
        return result
    finally:
        keep = str(os.getenv("GALLERY_KEEP_WORKDIR", "0")).strip().lower() in {"1", "true", "yes", "on"}
        if not keep:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
