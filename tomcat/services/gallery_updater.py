"""Build and publish a refreshed DINOv3 gallery from labeled local-photo crops."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
import contextlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
import torch
from torch import Tensor

from ..config import settings
from ..logger import log_action
from ..services import labeler_cache
from ..services import local_photos
from ..services.vision_feedback import load_verified_gallery_records
from ..vision.vision import DINOv3Wrapper, refresh_gallery


# Local photo metadata columns (0-based).
COL_URL = 0
COL_SERIAL = 6
COL_BOX_COORDS = 7
COL_BOX_CAT_IDS = 8

SN_PATTERN = re.compile(r"sn(\d+)", re.IGNORECASE)
_REJECTED = {"rejected", "needsreview", "needs review", "0. notacat", "notacat"}
_DEFAULT_MIN_PIXELS = int(os.getenv("GALLERY_MIN_PIXELS", "122500") or "122500")
_DEFAULT_MIN_PER_CAT = int(os.getenv("GALLERY_MIN_PER_CAT", "4") or "4")
_DEFAULT_BATCH_SIZE = int(os.getenv("GALLERY_EMBED_BATCH_SIZE", "32") or "32")
_DEFAULT_EMBED_BATCH_MAX = max(1, int(os.getenv("GALLERY_EMBED_BATCH_MAX", "256") or "256"))
_DEFAULT_TTA_HFLIP = str(os.getenv("GALLERY_TTA_HFLIP", "0")).strip().lower() in {"1", "true", "yes", "on"}
_DEFAULT_PROGRESS_LOG_SEC = max(5.0, float(os.getenv("GALLERY_PROGRESS_LOG_SEC", "15") or "15"))
_DEFAULT_USE_LOCAL_PHOTOS = str(os.getenv("GALLERY_USE_LOCAL_PHOTOS", "1")).strip().lower() in {"1", "true", "yes", "on"}
_DEFAULT_OVERWRITE_ACTIVE_VERSION = str(os.getenv("GALLERY_OVERWRITE_ACTIVE_VERSION", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _default_download_workers() -> int:
    raw = str(os.getenv("GALLERY_DOWNLOAD_WORKERS", "") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except Exception:
            pass
    cpu_total = int(os.cpu_count() or 8)
    # Use most of the machine during dedicated retrain windows, but keep some headroom.
    return max(4, min(24, int(round(cpu_total * 0.75))))


def _default_download_chunk_size(workers: int) -> int:
    raw = str(os.getenv("GALLERY_DOWNLOAD_CHUNK_SIZE", "") or "").strip()
    if raw:
        try:
            return max(16, int(raw))
        except Exception:
            pass
    return max(128, min(1024, int(max(1, workers) * 32)))


_DEFAULT_DOWNLOAD_WORKERS = _default_download_workers()
_DEFAULT_DOWNLOAD_CHUNK_SIZE = _default_download_chunk_size(_DEFAULT_DOWNLOAD_WORKERS)
_VERSIONED_GALLERY_RE = re.compile(r"^R(\d+(?:\.\d+)*)_cat_DINOv3_gallery\.pt$", re.IGNORECASE)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalize_gallery_version(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.lower().startswith("r"):
        text = text[1:].strip()
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        raise ValueError(f"Invalid gallery version: {raw}")
    return text


def _gallery_filename_for_version(version: str) -> str:
    return f"R{str(version).strip()}_cat_DINOv3_gallery.pt"


def _gallery_version_key(path: Path) -> Optional[Tuple[int, ...]]:
    match = _VERSIONED_GALLERY_RE.match(path.name)
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except Exception:
        return None


def _gallery_path_for_env(path: Path) -> str:
    root = _project_root()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path.resolve()).replace("\\", "/")


def _read_cv_gallery_path_from_env() -> Dict[str, Any]:
    env_path = _project_root() / ".env"
    out: Dict[str, Any] = {
        "ok": False,
        "found": False,
        "env_path": str(env_path),
        "value": "",
    }
    try:
        if not env_path.exists():
            out["error"] = ".env not found"
            return out
        raw = env_path.read_text(encoding="utf-8")
        for line in raw.splitlines():
            if not re.match(r"^\s*CV_GALLERY_PATH\s*=", line):
                continue
            out["found"] = True
            before_hash = line.partition("#")[0]
            _, _, value = before_hash.partition("=")
            out["value"] = str(value or "").strip()
            break
        out["ok"] = True
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def _cv_gallery_path_should_track_latest(raw_value: str) -> bool:
    raw = str(raw_value or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    if lowered in {"auto", "latest", "latest_local"}:
        return True
    if any(ch in raw for ch in "*?[]"):
        return True
    return False


def _summarize_cv_gallery_env_state(new_gallery_path: str) -> Dict[str, Any]:
    env_current = _read_cv_gallery_path_from_env()
    out: Dict[str, Any] = {
        "env_gallery_path": str(new_gallery_path),
        "env_gallery_env_file": str(env_current.get("env_path") or ""),
        "env_gallery_env_updated": False,
        "env_gallery_env_found": bool(env_current.get("found")),
        "env_gallery_env_previous": str(env_current.get("value") or ""),
        "env_gallery_env_tracking_latest": _cv_gallery_path_should_track_latest(
            str(env_current.get("value") or "")
        ),
        "env_gallery_env_preserved": True,
        "env_gallery_env_skip_reason": "preserved_by_design",
    }
    if not bool(env_current.get("ok")):
        out["env_gallery_env_error"] = str(env_current.get("error") or "unknown")
    return out


def _parse_serial(val: str) -> Optional[int]:
    sval = str(val or "").strip()
    m = SN_PATTERN.search(sval)
    if m:
        return int(m.group(1))
    if sval.isdigit():
        return int(sval)
    return None


def _parse_positive_int(val: Any) -> Optional[int]:
    try:
        parsed = int(val)
    except Exception:
        return None
    return parsed if parsed > 0 else None


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


def _get_image_bytes(
    serial: int,
    *,
    use_local_photos: bool = _DEFAULT_USE_LOCAL_PHOTOS,
) -> Tuple[Optional[bytes], Optional[str], str]:
    # Fast path: labeler cache.
    try:
        cached = labeler_cache.get_cached_image(int(serial))
        if cached:
            return cached, None, "labeler_cache"
    except Exception:
        pass

    # Preferred path: manually supervised local photo store.
    if use_local_photos:
        try:
            local_path = local_photos.get_local_photo_path(int(serial))
            if local_path and local_path.is_file():
                return None, str(local_path), "local_photo"
        except Exception:
            pass

    return None, None, "local_missing"


def _build_row_crops(
    serial: int,
    coords: List[str],
    labels: List[str],
    *,
    image_bytes: Optional[bytes],
    image_path: Optional[str],
    crop_root: Path,
    min_pixels: int,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "serial": int(serial),
        "saved": [],
        "crop_candidates": 0,
        "crop_filtered_small": 0,
    }
    try:
        if image_path:
            image = Image.open(image_path).convert("RGB")
        elif image_bytes:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        else:
            return out
    except Exception:
        return out

    iw, ih = image.size
    limit = min(len(coords), len(labels))
    saved: List[Tuple[str, str, str]] = []
    filtered_small = 0
    for idx in range(limit):
        out["crop_candidates"] += 1
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
            filtered_small += 1
            continue
        crop = image.crop((ex1, ey1, ex2, ey2))
        crop_id = f"sn{int(serial):04d}_c{idx + 1:02d}"
        cat_slug = _slug(cat_name)
        cat_dir = crop_root / cat_slug
        try:
            cat_dir.mkdir(parents=True, exist_ok=True)
            crop_path = cat_dir / f"{crop_id}.jpg"
            crop.save(crop_path, format="JPEG", quality=95)
        except Exception:
            continue
        saved.append({
            "cat_name": str(cat_name),
            "crop_id": str(crop_id),
            "crop_path": str(crop_path),
            "serial": int(serial),
            "crop": int(idx + 1),
            "source": "photo_metadata",
        })
    out["saved"] = saved
    out["crop_filtered_small"] = int(filtered_small)
    return out


def _next_version_path(weights_dir: Path) -> Path:
    highest_key: Optional[Tuple[int, ...]] = None
    if weights_dir.exists():
        for p in weights_dir.iterdir():
            key = _gallery_version_key(p)
            if key is None:
                continue
            if highest_key is None or key > highest_key:
                highest_key = key
    if highest_key is None:
        next_key = (4, 5, 1)
    elif len(highest_key) == 1:
        next_key = (highest_key[0], 0, 1)
    elif len(highest_key) == 2:
        next_key = (*highest_key, 1)
    else:
        next_key = (*highest_key[:-1], highest_key[-1] + 1)
    next_version = ".".join(str(part) for part in next_key)
    return weights_dir / _gallery_filename_for_version(next_version)


def _is_versioned_gallery_path(path: Path) -> bool:
    return bool(_VERSIONED_GALLERY_RE.match(path.name))


def _prune_old_versioned_galleries(weights_dir: Path, keep_version_path: Path) -> int:
    """Keep only the newest auto-generated patch gallery for the current release line."""
    removed = 0
    if not weights_dir.exists():
        return 0
    keep_key = _gallery_version_key(keep_version_path)
    keep_prefix = keep_key[:-1] if keep_key and len(keep_key) >= 3 else None
    keep_resolved = keep_version_path.resolve()
    for p in weights_dir.iterdir():
        key = _gallery_version_key(p)
        if key is None or len(key) < 3 or key[-1] < 0:
            continue
        if keep_prefix is None or key[:-1] != keep_prefix:
            continue
        try:
            if p.resolve() == keep_resolved:
                continue
        except Exception:
            if p == keep_version_path:
                continue
        try:
            p.unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass
    return removed


def _load_rows() -> List[List[str]]:
    return local_photos.read_metadata_table()


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


def _initial_embed_batch_size(device: torch.device, base_batch: int) -> int:
    batch = max(1, int(base_batch))
    if device.type != "cuda":
        return batch
    try:
        free_bytes, _ = torch.cuda.mem_get_info(device)
    except Exception:
        return batch
    free_gib = float(free_bytes) / float(1024 ** 3)
    if free_gib >= 5.0:
        batch *= 8
    elif free_gib >= 3.5:
        batch *= 6
    elif free_gib >= 2.5:
        batch *= 4
    elif free_gib >= 1.5:
        batch *= 2
    return max(1, min(int(batch), int(_DEFAULT_EMBED_BATCH_MAX)))


def run_gallery_update(
    *,
    mode: str = "full",
    gallery_version: Optional[str] = None,
    min_pixels: int = _DEFAULT_MIN_PIXELS,
    min_per_cat: int = _DEFAULT_MIN_PER_CAT,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    tta_hflip: bool = _DEFAULT_TTA_HFLIP,
    use_local_photos: bool = _DEFAULT_USE_LOCAL_PHOTOS,
    download_workers: int = _DEFAULT_DOWNLOAD_WORKERS,
    download_chunk_size: int = _DEFAULT_DOWNLOAD_CHUNK_SIZE,
    progress_log_sec: float = _DEFAULT_PROGRESS_LOG_SEC,
) -> Dict[str, Any]:
    """Rebuild the gallery from local metadata labels and publish the newest versioned gallery.

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
        "gallery_version_requested": str(gallery_version or "").strip(),
        "tta_hflip": bool(tta_hflip),
        "download_workers": int(max(1, download_workers)),
        "download_chunk_size": int(max(16, download_chunk_size)),
        "progress_log_sec": float(max(5.0, progress_log_sec)),
        "use_local_photos": bool(use_local_photos),
        "local_photo_root": str(local_photos.photo_root()),
        "rows": 0,
        "rows_with_boxes": 0,
        "rows_processed": 0,
        "crop_candidates": 0,
        "crop_saved": 0,
        "crop_filtered_small": 0,
        "images_requested": 0,
        "images_loaded": 0,
        "images_loaded_cache": 0,
        "images_loaded_local": 0,
        "images_missing_local": 0,
        "images_failed": 0,
        "cats_before_filter": 0,
        "cats_after_filter": 0,
        "verified_records": 0,
        "verified_used": 0,
        "verified_cats_included": 0,
    }

    cat_to_crops: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    unique_crops: set[str] = set()
    verified_priority_cats: set[str] = set()
    started_mono = time.monotonic()
    progress_every = float(max(5.0, progress_log_sec))
    next_progress_at = started_mono + progress_every

    def _log_progress(stage: str, *, force: bool = False, extra: str = "") -> None:
        nonlocal next_progress_at
        now = time.monotonic()
        if not force and now < next_progress_at:
            return
        elapsed = now - started_mono
        tail = (
            f"elapsed={elapsed:.1f}s rows={int(stats.get('rows_processed', 0))}/{int(stats.get('rows_with_boxes', 0))} "
            f"crops={int(stats.get('crop_saved', 0))}/{int(stats.get('crop_candidates', 0))} "
            f"images={int(stats.get('images_loaded', 0))}/{int(stats.get('images_requested', 0))}"
        )
        if extra:
            tail = f"{tail}; {extra}"
        log_action("gallery_updater_progress", stage, tail)
        next_progress_at = now + progress_every

    try:
        rows = _load_rows()
        stats["rows"] = max(0, len(rows) - 1)
        row_jobs: List[Tuple[int, List[str], List[str]]] = []
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            serial = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if serial is None:
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
            row_jobs.append((int(serial), coords, labels))

        workers = int(max(1, download_workers))
        chunk_size = int(max(16, download_chunk_size))
        if row_jobs:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for start in range(0, len(row_jobs), chunk_size):
                    chunk = row_jobs[start:start + chunk_size]
                    req_by_serial: Dict[int, None] = {}
                    for serial, _, _ in chunk:
                        req_by_serial.setdefault(int(serial), None)
                    stats["images_requested"] += int(len(req_by_serial))

                    future_map = {
                        pool.submit(
                            _get_image_bytes,
                            int(serial),
                            use_local_photos=bool(use_local_photos),
                        ): int(serial)
                        for serial, url in req_by_serial.items()
                    }
                    image_ref_by_serial: Dict[int, Tuple[Optional[bytes], Optional[str], str]] = {}
                    for fut in as_completed(future_map):
                        serial = future_map[fut]
                        source = ""
                        try:
                            data, image_path, source = fut.result()
                        except Exception:
                            data = None
                            image_path = None
                        if data or image_path:
                            image_ref_by_serial[int(serial)] = (data, image_path, source)
                            stats["images_loaded"] += 1
                            if source == "labeler_cache":
                                stats["images_loaded_cache"] += 1
                            elif source == "local_photo":
                                stats["images_loaded_local"] += 1
                        else:
                            stats["images_failed"] += 1
                            if source == "local_missing":
                                stats["images_missing_local"] += 1

                    crop_future_map = {}
                    for serial, coords, labels in chunk:
                        image_ref = image_ref_by_serial.get(int(serial))
                        if not image_ref:
                            stats["rows_processed"] += 1
                            _log_progress("crop-build")
                            continue
                        image_bytes, image_path, _ = image_ref
                        crop_future_map[
                            pool.submit(
                                _build_row_crops,
                                int(serial),
                                coords,
                                labels,
                                image_bytes=image_bytes,
                                image_path=image_path,
                                crop_root=crop_root,
                                min_pixels=int(min_pixels),
                            )
                        ] = int(serial)

                    for fut in as_completed(crop_future_map):
                        serial = crop_future_map[fut]
                        stats["rows_processed"] += 1
                        try:
                            crop_result = fut.result()
                        except Exception:
                            _log_progress("crop-build")
                            continue
                        stats["crop_candidates"] += int(crop_result.get("crop_candidates", 0) or 0)
                        stats["crop_filtered_small"] += int(crop_result.get("crop_filtered_small", 0) or 0)
                        for item in crop_result.get("saved") or []:
                            if not isinstance(item, dict):
                                continue
                            cat_name = str(item.get("cat_name") or "").strip()
                            crop_id = str(item.get("crop_id") or "").strip()
                            crop_path_str = str(item.get("crop_path") or "").strip()
                            if not cat_name or not crop_id or not crop_path_str:
                                continue
                            unique_key = f"{str(cat_name).casefold()}::{crop_id}"
                            if unique_key in unique_crops:
                                continue
                            unique_crops.add(unique_key)
                            cat_to_crops[str(cat_name)].append({
                                "cat_name": str(cat_name),
                                "crop_id": str(crop_id),
                                "crop_path": Path(str(crop_path_str)),
                                "serial": int(item.get("serial") or 0) or None,
                                "crop": int(item.get("crop") or 0) or None,
                                "source": str(item.get("source") or "photo_metadata"),
                            })
                            stats["crop_saved"] += 1
                        _log_progress("crop-build")

        _log_progress("crop-build", force=True, extra="stage_complete=1")

        # High-priority source: human-verified Discord reactions.
        # These are included even if the cat has fewer than min_per_cat sheet crops.
        verified_records = load_verified_gallery_records()
        stats["verified_records"] = int(len(verified_records))
        for rec in verified_records:
            cat_name = _normalize_label(str(rec.get("cat_name") or ""))
            crop_path = Path(rec.get("crop_path"))
            if not cat_name or not crop_path.exists():
                continue
            rec_id = str(rec.get("id") or crop_path.stem)
            unique_key = f"{cat_name.casefold()}::{rec_id}"
            if unique_key in unique_crops:
                continue
            unique_crops.add(unique_key)
            cat_to_crops[cat_name].append({
                "cat_name": str(cat_name),
                "crop_id": str(rec_id),
                "crop_path": crop_path,
                "serial": _parse_positive_int(rec.get("serial")),
                "crop": _parse_positive_int(rec.get("crop")),
                "source": str(rec.get("source") or "discord_correct"),
            })
            verified_priority_cats.add(cat_name)
            stats["verified_used"] += 1

        stats["cats_before_filter"] = len(cat_to_crops)
        eligible_cats = sorted([
            c for c, items in cat_to_crops.items()
            if len(items) >= int(min_per_cat) or c in verified_priority_cats
        ])
        stats["cats_after_filter"] = len(eligible_cats)
        stats["verified_cats_included"] = sum(1 for c in eligible_cats if c in verified_priority_cats)
        if not eligible_cats:
            raise RuntimeError("No eligible cats after quality filtering")

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = _load_encoder(device)
        embed_batch_size = _initial_embed_batch_size(device, int(batch_size))
        stats["embed_batch_requested"] = int(batch_size)
        stats["embed_batch_initial"] = int(embed_batch_size)
        stats["embed_batch_max"] = int(_DEFAULT_EMBED_BATCH_MAX)
        stats["embed_autocast_fp16"] = int(device.type == "cuda")

        class_to_idx = {cat: i for i, cat in enumerate(eligible_cats)}
        all_emb: List[Tensor] = []
        all_labels: List[int] = []
        all_paths: List[str] = []
        all_records: List[Dict[str, Any]] = []
        expected_embeddings = sum(len(cat_to_crops.get(cat, [])) for cat in eligible_cats)
        embedded_done = 0

        for cat in eligible_cats:
            items = cat_to_crops[cat]
            cat_idx = class_to_idx[cat]
            start = 0
            while start < len(items):
                cur_batch_size = int(max(1, embed_batch_size))
                batch = items[start:start + cur_batch_size]
                tensors: List[Tensor] = []
                batch_items: List[Dict[str, Any]] = []
                for item in batch:
                    crop_id = str(item.get("crop_id") or "").strip()
                    crop_path = Path(item.get("crop_path"))
                    if not crop_id:
                        continue
                    try:
                        img = Image.open(crop_path).convert("RGB")
                    except Exception:
                        continue
                    tensors.append(_prep_tensor(img))
                    batch_items.append(item)
                if not tensors:
                    start += len(batch)
                    continue
                while True:
                    batch_t = torch.stack(tensors).to(device, non_blocking=(device.type == "cuda"))
                    try:
                        with torch.inference_mode():
                            autocast_ctx = (
                                torch.autocast(device_type="cuda", dtype=torch.float16)
                                if device.type == "cuda"
                                else contextlib.nullcontext()
                            )
                            with autocast_ctx:
                                emb = model(batch_t)
                                if tta_hflip:
                                    emb_flip = model(torch.flip(batch_t, dims=[3]))
                                    emb = (emb + emb_flip) / 2.0
                            emb = torch.nn.functional.normalize(emb, p=2, dim=1)
                            emb = emb.detach().float().cpu()
                        break
                    except RuntimeError as e:
                        oom = device.type == "cuda" and "out of memory" in str(e).lower()
                        if not oom or cur_batch_size <= 1:
                            raise
                        try:
                            torch.cuda.empty_cache()
                        except Exception:
                            pass
                        cur_batch_size = max(1, cur_batch_size // 2)
                        embed_batch_size = cur_batch_size
                        stats["embed_batch_initial"] = min(
                            int(stats.get("embed_batch_initial", cur_batch_size)),
                            int(cur_batch_size),
                        )
                        batch = items[start:start + cur_batch_size]
                        tensors = []
                        batch_items = []
                        for item in batch:
                            crop_id = str(item.get("crop_id") or "").strip()
                            crop_path = Path(item.get("crop_path"))
                            if not crop_id:
                                continue
                            try:
                                img = Image.open(crop_path).convert("RGB")
                            except Exception:
                                continue
                            tensors.append(_prep_tensor(img))
                            batch_items.append(item)
                        if not tensors:
                            break
                        continue
                    finally:
                        try:
                            del batch_t
                        except Exception:
                            pass
                if not tensors:
                    start += len(batch)
                    continue
                all_emb.append(emb)
                all_labels.extend([cat_idx] * emb.shape[0])
                embedded_done += int(emb.shape[0])
                for item in batch_items:
                    crop_id = str(item.get("crop_id") or "").strip()
                    serial = int(item.get("serial") or 0) or None
                    crop_num = int(item.get("crop") or 0) or None
                    crop_uri = f"crop://{crop_id}:{cat}" if crop_id else ""
                    all_paths.append(crop_uri)
                    all_records.append({
                        "cat_name": str(cat),
                        "crop_id": crop_id,
                        "path": crop_uri,
                        "serial": serial,
                        "crop": crop_num,
                        "source": str(item.get("source") or "gallery_update"),
                    })
                start += len(batch_items)
                stats["embed_batch_effective"] = int(embed_batch_size)
                _log_progress(
                    "embedding",
                    extra=f"embedded={embedded_done}/{expected_embeddings}; cats={len(class_to_idx)}; batch={embed_batch_size}",
                )

        _log_progress(
            "embedding",
            force=True,
            extra=f"stage_complete=1; embedded={embedded_done}/{expected_embeddings}",
        )

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
            "records": all_records,
            "record_schema": "sn_crop_v1",
        }

        previous_active_gallery_path = Path(settings.cv_gallery_path)
        weights_dir = previous_active_gallery_path.parent if previous_active_gallery_path.parent else Path("weights")
        weights_dir.mkdir(parents=True, exist_ok=True)
        overwrite_active_version = bool(_DEFAULT_OVERWRITE_ACTIVE_VERSION)
        requested_gallery_version = _normalize_gallery_version(gallery_version)
        if requested_gallery_version:
            version_path = weights_dir / _gallery_filename_for_version(requested_gallery_version)
        elif overwrite_active_version and _is_versioned_gallery_path(previous_active_gallery_path):
            version_path = previous_active_gallery_path
        else:
            version_path = _next_version_path(weights_dir)

        # Write only the versioned gallery; baseline galleries remain untouched.
        torch.save(gallery_obj, version_path)
        removed_old_versions = _prune_old_versioned_galleries(weights_dir, version_path)
        stats["old_versions_pruned"] = int(removed_old_versions)
        stats["overwrite_active_version"] = int(overwrite_active_version)
        stats["gallery_version_written"] = version_path.name

        env_gallery_path = _gallery_path_for_env(version_path)
        stats.update(_summarize_cv_gallery_env_state(env_gallery_path))

        # Persist the latest crop tree so crop:// gallery refs can resolve locally
        # without rebuilding thumbs from source images at runtime.
        active_crops_root = Path("cache") / "gallery_retrain" / "active_crops"
        try:
            active_crops_root.parent.mkdir(parents=True, exist_ok=True)
            if active_crops_root.exists():
                shutil.rmtree(active_crops_root, ignore_errors=True)
            if crop_root.exists():
                try:
                    crop_root.replace(active_crops_root)
                except Exception:
                    shutil.copytree(crop_root, active_crops_root)
                stats["active_crops_path"] = str(active_crops_root)
        except Exception as e:
            log_action("gallery_updater_active_crops_error", "error", str(e))

        # Refresh the live gallery pointer as soon as the build completes.
        refresh_state = refresh_gallery(str(version_path.resolve()))

        result = {
            "status": "ok",
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
            "run_date": datetime.now().date().isoformat(),
            "active_gallery_path": str(version_path),
            "previous_active_gallery_path": str(previous_active_gallery_path),
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
            f"active={version_path.name} tta_hflip={int(bool(tta_hflip))}",
        )
        return result
    finally:
        keep = str(os.getenv("GALLERY_KEEP_WORKDIR", "0")).strip().lower() in {"1", "true", "yes", "on"}
        if not keep:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
