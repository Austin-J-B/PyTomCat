"""Persist Discord CV feedback for gallery retrain and local photo metadata continuity."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image

from ..logger import log_action
from . import local_photos

_CACHE_ROOT = Path("cache")
_DISCORD_ROOT = _CACHE_ROOT / "discord"

_PENDING_META_DIR = _DISCORD_ROOT / "pending" / "meta"
_PENDING_IMG_DIR = _DISCORD_ROOT / "pending" / "images"
_CORRECT_RECORDS_DIR = _DISCORD_ROOT / "correct" / "records"
_CORRECT_CROPS_DIR = _DISCORD_ROOT / "correct" / "crops"
_INCORRECT_RECORDS_DIR = _DISCORD_ROOT / "incorrect" / "records"
_INCORRECT_IMAGES_DIR = _DISCORD_ROOT / "incorrect" / "images"

# Legacy layout retained for migration + backward compatibility.
_LEGACY_PENDING_META_DIR = _CACHE_ROOT / "discord_feedback" / "pending"
_LEGACY_PENDING_IMG_DIR = _CACHE_ROOT / "discord_feedback" / "images"
_LEGACY_CORRECT_RECORDS_DIR = _CACHE_ROOT / "discord_verified" / "records"
_LEGACY_CORRECT_CROPS_DIR = _CACHE_ROOT / "discord_verified" / "crops"
_LEGACY_INCORRECT_RECORDS_DIR = _CACHE_ROOT / "discord_disputed" / "records"
_LEGACY_INCORRECT_IMAGES_DIR = _CACHE_ROOT / "discord_disputed" / "images"

_PENDING_TTL_SEC = max(3600, int(os.getenv("VISION_FEEDBACK_PENDING_TTL_SEC", "259200") or "259200"))
_APPROVALS_REQUIRED = max(1, int(os.getenv("VISION_FEEDBACK_APPROVALS_REQUIRED", "1") or "1"))
_CHECKMARK = "\u2705"
_CROSSMARK = "\u274c"
_BOT_LABELED_BY = "tomcat-identify"

_DIRS_READY = False


def _ensure_dirs() -> None:
    global _DIRS_READY
    if not _DIRS_READY:
        _migrate_legacy_layout()
        _DIRS_READY = True
    _PENDING_META_DIR.mkdir(parents=True, exist_ok=True)
    _PENDING_IMG_DIR.mkdir(parents=True, exist_ok=True)
    _CORRECT_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    _CORRECT_CROPS_DIR.mkdir(parents=True, exist_ok=True)
    _INCORRECT_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    _INCORRECT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    _purge_expired_pending()


def _migrate_one_dir(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.glob("*"):
        target = dst / item.name
        if target.exists():
            continue
        try:
            shutil.move(str(item), str(target))
        except Exception:
            pass
    try:
        src.rmdir()
    except Exception:
        pass


def _migrate_legacy_layout() -> None:
    _migrate_one_dir(_LEGACY_PENDING_META_DIR, _PENDING_META_DIR)
    _migrate_one_dir(_LEGACY_PENDING_IMG_DIR, _PENDING_IMG_DIR)
    _migrate_one_dir(_LEGACY_CORRECT_RECORDS_DIR, _CORRECT_RECORDS_DIR)
    _migrate_one_dir(_LEGACY_CORRECT_CROPS_DIR, _CORRECT_CROPS_DIR)
    _migrate_one_dir(_LEGACY_INCORRECT_RECORDS_DIR, _INCORRECT_RECORDS_DIR)
    _migrate_one_dir(_LEGACY_INCORRECT_IMAGES_DIR, _INCORRECT_IMAGES_DIR)
    for old_root in (_CACHE_ROOT / "discord_feedback", _CACHE_ROOT / "discord_verified", _CACHE_ROOT / "discord_disputed"):
        try:
            old_root.rmdir()
        except Exception:
            pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(text: str) -> str:
    out = []
    for ch in str(text or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {" ", "-", "_", "."}:
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "cat"


def _normalize_box(raw_box: Any) -> List[float] | None:
    if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(v) for v in raw_box]
    except Exception:
        return None
    return [x1, y1, x2, y2]


def _clamp_abs_box(box: List[float], img_w: int, img_h: int) -> Tuple[int, int, int, int] | None:
    x1, y1, x2, y2 = box
    x1 = max(0, min(int(round(x1)), img_w))
    x2 = max(0, min(int(round(x2)), img_w))
    y1 = max(0, min(int(round(y1)), img_h))
    y2 = max(0, min(int(round(y2)), img_h))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _pending_meta_path(reply_message_id: int) -> Path:
    return _PENDING_META_DIR / f"{int(reply_message_id)}.json"


def _legacy_pending_meta_path(reply_message_id: int) -> Path:
    return _LEGACY_PENDING_META_DIR / f"{int(reply_message_id)}.json"


def _pending_image_path(reply_message_id: int) -> Path:
    return _PENDING_IMG_DIR / f"{int(reply_message_id)}.jpg"


def _legacy_pending_image_path(reply_message_id: int) -> Path:
    return _LEGACY_PENDING_IMG_DIR / f"{int(reply_message_id)}.jpg"


def _find_existing_pending_meta_path(reply_message_id: int) -> Path:
    p_new = _pending_meta_path(reply_message_id)
    if p_new.exists():
        return p_new
    p_old = _legacy_pending_meta_path(reply_message_id)
    if p_old.exists():
        return p_old
    return p_new


def _resolve_pending_image_path(meta: Dict[str, Any]) -> Path:
    raw = str(meta.get("image_path") or "").strip()
    if raw:
        p = Path(raw)
        if p.exists():
            return p
    msg_id = int(meta.get("reply_message_id") or 0)
    if msg_id > 0:
        p_new = _pending_image_path(msg_id)
        if p_new.exists():
            return p_new
        p_old = _legacy_pending_image_path(msg_id)
        if p_old.exists():
            return p_old
    if raw:
        name = Path(raw).name
        if name:
            cand = _PENDING_IMG_DIR / name
            if cand.exists():
                return cand
            cand_old = _LEGACY_PENDING_IMG_DIR / name
            if cand_old.exists():
                return cand_old
    return Path(raw) if raw else Path("")


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _dump_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _purge_expired_pending() -> None:
    now = time.time()
    live_ids: set[int] = set()
    pending_dirs = [_PENDING_META_DIR, _LEGACY_PENDING_META_DIR]
    for pending_dir in pending_dirs:
        for meta_path in pending_dir.glob("*.json"):
            meta = _load_json(meta_path)
            ts = float(meta.get("created_ts") or 0.0)
            try:
                msg_id = int(meta.get("reply_message_id") or int(meta_path.stem))
            except Exception:
                msg_id = 0
            is_finalized = bool(meta.get("finalized"))
            is_expired = ts > 0 and (now - ts) > _PENDING_TTL_SEC
            if not is_finalized and not is_expired:
                if msg_id > 0:
                    live_ids.add(msg_id)
                continue
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass
            if msg_id > 0:
                try:
                    _pending_image_path(msg_id).unlink(missing_ok=True)
                    _legacy_pending_image_path(msg_id).unlink(missing_ok=True)
                except Exception:
                    pass

    for image_dir in (_PENDING_IMG_DIR, _LEGACY_PENDING_IMG_DIR):
        for image_path in image_dir.glob("*.jpg"):
            try:
                msg_id = int(image_path.stem)
            except Exception:
                continue
            if msg_id in live_ids:
                continue
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass


def _meta_serial(meta: Dict[str, Any]) -> int:
    """Return the local serial, honoring legacy pending files that still carry sheet_serial."""
    for key in ("serial", "sheet_serial"):
        try:
            value = int(meta.get(key) or 0)
        except Exception:
            value = 0
        if value > 0:
            return int(value)
    return 0


def _ensure_feedback_serial(meta: Dict[str, Any]) -> int:
    serial = _meta_serial(meta)
    if serial > 0 and local_photos.has_local_photo(serial):
        meta["serial"] = int(serial)
        return int(serial)
    img_path = _resolve_pending_image_path(meta)
    if not img_path.exists():
        return int(serial or 0)
    try:
        image_bytes = img_path.read_bytes()
    except Exception:
        return int(serial or 0)
    result = local_photos.upsert_photo_bytes(
        image_bytes,
        discord_url=str(meta.get("source_image_url") or "").strip(),
        timestamp=str(meta.get("source_created_at") or "").strip(),
        author_id=str(meta.get("source_author_id") or "").strip(),
        channel=str(meta.get("source_channel_id") or "").strip(),
        guild_id=str(meta.get("guild_id") or "").strip(),
        message_id=str(meta.get("source_message_id") or "").strip(),
        filename=str(meta.get("source_filename") or "").strip(),
        content_type=str(meta.get("source_content_type") or "").strip(),
    )
    try:
        serial = int(result.get("serial") or 0)
    except Exception:
        serial = 0
    if serial > 0:
        meta["serial"] = int(serial)
    return int(serial)


def _build_metadata_labels(meta: Dict[str, Any]) -> Tuple[str, str]:
    img_path = _resolve_pending_image_path(meta)
    if not img_path.exists():
        return "", ""
    try:
        with Image.open(img_path) as img:
            iw, ih = img.size
    except Exception:
        return "", ""
    if iw <= 0 or ih <= 0:
        return "", ""

    results = sorted(
        list(meta.get("results") or []),
        key=lambda r: int((r or {}).get("index") or 0),
    )
    coords: List[str] = []
    labels: List[str] = []
    for r in results:
        label = str((r or {}).get("name") or "").strip()
        if not label:
            continue
        box = _normalize_box((r or {}).get("box"))
        if box is None:
            continue
        clamped = _clamp_abs_box(box, iw, ih)
        if clamped is None:
            continue
        x1, y1, x2, y2 = clamped
        w = (x2 - x1) / float(iw)
        h = (y2 - y1) / float(ih)
        cx = ((x1 + x2) / 2.0) / float(iw)
        cy = ((y1 + y2) / 2.0) / float(ih)
        coords.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        labels.append(label)
    if not coords or not labels:
        return "", ""
    return "|".join(coords), "|".join(labels)


def _write_verified_labels(meta: Dict[str, Any]) -> int:
    box_coords, box_cat_ids = _build_metadata_labels(meta)
    if not box_coords or not box_cat_ids:
        return 0
    serial = _ensure_feedback_serial(meta)
    if serial <= 0:
        return 0
    local_photos.update_metadata_annotations(
        [{
            "serial": int(serial),
            "box_coords": box_coords,
            "box_cat_ids": box_cat_ids,
        }],
        _BOT_LABELED_BY,
    )
    return len([t for t in box_cat_ids.split("|") if str(t).strip()])


def _mark_metadata_incorrect(meta: Dict[str, Any]) -> int:
    serial = _ensure_feedback_serial(meta)
    if serial <= 0:
        return 0
    outcome = local_photos.clear_metadata_annotations([int(serial)], _BOT_LABELED_BY)
    result = (outcome.get("results") or {}).get(int(serial)) or {}
    return 1 if bool(result.get("ok")) else 0


def register_identify_feedback(
    *,
    reply_message_id: int,
    reply_channel_id: int,
    source_message_id: int,
    source_channel_id: int,
    guild_id: int | None,
    image_bytes: bytes,
    results: List[Dict[str, Any]],
    source_image_url: str = "",
    source_author_id: str = "",
    source_username: str = "",
    source_created_at: str = "",
    source_filename: str = "",
    source_content_type: str = "",
) -> None:
    """Persist identify result context and ensure the source image has a local serial."""
    if not image_bytes or not results:
        return
    try:
        _ensure_dirs()
        _purge_expired_pending()
        clean_results: List[Dict[str, Any]] = []
        for r in results:
            box = _normalize_box((r or {}).get("box"))
            if box is None:
                continue
            clean_top5: List[Dict[str, Any]] = []
            for cand in (r or {}).get("top5") or []:
                if not isinstance(cand, (list, tuple)) or len(cand) != 2:
                    continue
                name = str(cand[0] or "").strip()
                try:
                    conf = float(cand[1])
                except Exception:
                    conf = 0.0
                if name:
                    clean_top5.append({"name": name, "conf": conf})
            name = str((r or {}).get("name") or "").strip()
            try:
                conf = float((r or {}).get("conf") or 0.0)
            except Exception:
                conf = 0.0
            clean_results.append({
                "index": int((r or {}).get("index") or 0),
                "name": name,
                "conf": conf,
                "box": box,
                "top5": clean_top5,
            })
        if not clean_results:
            return

        img_path = _pending_image_path(int(reply_message_id))
        img_path.write_bytes(image_bytes)

        payload = {
            "created_ts": time.time(),
            "created_at": _utc_now_iso(),
            "reply_message_id": int(reply_message_id),
            "reply_channel_id": int(reply_channel_id),
            "source_message_id": int(source_message_id),
            "source_channel_id": int(source_channel_id),
            "guild_id": int(guild_id or 0),
            "source_image_url": str(source_image_url or "").strip(),
            "source_author_id": str(source_author_id or "").strip(),
            "source_username": str(source_username or "").strip(),
            "source_created_at": str(source_created_at or "").strip(),
            "source_filename": str(source_filename or "").strip(),
            "source_content_type": str(source_content_type or "").strip(),
            "image_path": str(img_path),
            "results": clean_results,
            "votes": {"correct": [], "incorrect": []},
            "finalized": False,
            "final_emoji": "",
            "finalized_at": "",
            "finalized_by": 0,
        }
        _dump_json(_pending_meta_path(int(reply_message_id)), payload)
    except Exception as e:
        log_action("discord_feedback_register_error", f"msg={reply_message_id}", str(e))


def _save_correct_crops(meta: Dict[str, Any], reactor_user_id: int, reactor_name: str) -> int:
    img_path = _resolve_pending_image_path(meta)
    if not img_path.exists():
        return 0
    try:
        with Image.open(img_path) as img:
            img = img.convert("RGB")
            iw, ih = img.size
            saved = 0
            msg_id = int(meta.get("reply_message_id") or 0)
            for r in meta.get("results") or []:
                box = _normalize_box((r or {}).get("box"))
                if box is None:
                    continue
                rect = _clamp_abs_box(box, iw, ih)
                if rect is None:
                    continue
                x1, y1, x2, y2 = rect
                crop = img.crop((x1, y1, x2, y2))
                cat_name = str((r or {}).get("name") or "").strip()
                if not cat_name:
                    continue
                rec_id = f"{int(time.time())}_m{msg_id}_c{int((r or {}).get('index') or (saved + 1)):02d}"
                crop_name = f"{rec_id}_{_safe_slug(cat_name)}.jpg"
                crop_path = _CORRECT_CROPS_DIR / crop_name
                try:
                    crop.save(crop_path, format="JPEG", quality=95)
                except Exception:
                    continue

                rec = {
                    "id": rec_id,
                    "created_at": _utc_now_iso(),
                    "source": "discord_reaction_correct",
                    "reply_message_id": msg_id,
                    "reply_channel_id": int(meta.get("reply_channel_id") or 0),
                    "source_message_id": int(meta.get("source_message_id") or 0),
                    "source_channel_id": int(meta.get("source_channel_id") or 0),
                    "guild_id": int(meta.get("guild_id") or 0),
                    "reacted_by_user_id": int(reactor_user_id or 0),
                    "reacted_by": str(reactor_name or "").strip(),
                    "cat_name": cat_name,
                    "pred_conf": float((r or {}).get("conf") or 0.0),
                    "box_abs": [x1, y1, x2, y2],
                    "crop_w": int(x2 - x1),
                    "crop_h": int(y2 - y1),
                    "crop_pixels": int((x2 - x1) * (y2 - y1)),
                    "crop_path": str(crop_path),
                    "top5": (r or {}).get("top5") or [],
                    "serial": int(_meta_serial(meta) or 0),
                }
                _dump_json(_CORRECT_RECORDS_DIR / f"{rec_id}.json", rec)
                saved += 1
            return saved
    except Exception:
        return 0


def _save_incorrect_record(meta: Dict[str, Any], reactor_user_id: int, reactor_name: str) -> int:
    msg_id = int(meta.get("reply_message_id") or 0)
    img_path = _resolve_pending_image_path(meta)
    if not img_path.exists():
        return 0
    try:
        out_img = _INCORRECT_IMAGES_DIR / f"m{msg_id}.jpg"
        if not out_img.exists():
            out_img.write_bytes(img_path.read_bytes())
        rec_id = f"{int(time.time())}_m{msg_id}"
        rec = {
            "id": rec_id,
            "created_at": _utc_now_iso(),
            "source": "discord_reaction_incorrect",
            "reply_message_id": msg_id,
            "reply_channel_id": int(meta.get("reply_channel_id") or 0),
            "source_message_id": int(meta.get("source_message_id") or 0),
            "source_channel_id": int(meta.get("source_channel_id") or 0),
            "guild_id": int(meta.get("guild_id") or 0),
            "reacted_by_user_id": int(reactor_user_id or 0),
            "reacted_by": str(reactor_name or "").strip(),
            "image_path": str(out_img),
            "results": meta.get("results") or [],
            "serial": int(_meta_serial(meta) or 0),
        }
        _dump_json(_INCORRECT_RECORDS_DIR / f"{rec_id}.json", rec)
        return len(rec.get("results") or [])
    except Exception:
        return 0


def _canonical_feedback_symbol(emoji: str) -> str:
    symbol = str(emoji or "").strip()
    if symbol in {_CHECKMARK, "\u2714", "\u2714\ufe0f"}:
        return _CHECKMARK
    if symbol in {_CROSSMARK, "\u274e", "\u2716", "\u2716\ufe0f"}:
        return _CROSSMARK
    return ""


def _append_vote(meta: Dict[str, Any], kind: str, user_id: int) -> int:
    votes = meta.setdefault("votes", {})
    items = votes.get(kind)
    if not isinstance(items, list):
        items = []
    uid = int(user_id or 0)
    if uid > 0 and uid not in items:
        items.append(uid)
    votes[kind] = items
    return len(items)


def process_identify_reaction(
    *,
    reply_message_id: int,
    emoji: str,
    reactor_user_id: int,
    reactor_name: str = "",
) -> bool:
    """Consume check/cross reactions on a CV identify reply. Returns True if handled."""
    symbol = _canonical_feedback_symbol(emoji)
    if not symbol:
        return False
    _ensure_dirs()
    meta_path = _find_existing_pending_meta_path(int(reply_message_id))
    if not meta_path.exists():
        return False
    meta = _load_json(meta_path)
    if not meta:
        return False
    resolved_img_path = _resolve_pending_image_path(meta)
    if resolved_img_path and str(meta.get("image_path") or "").strip() != str(resolved_img_path):
        meta["image_path"] = str(resolved_img_path)
        _dump_json(meta_path, meta)
    if bool(meta.get("finalized")):
        return True

    saved = 0
    metadata_count = 0
    if symbol == _CHECKMARK:
        vote_count = _append_vote(meta, "correct", int(reactor_user_id or 0))
        _dump_json(meta_path, meta)
        if vote_count < int(_APPROVALS_REQUIRED):
            log_action(
                "discord_feedback_correct_vote",
                f"msg={reply_message_id}; user={reactor_user_id}",
                f"votes={vote_count}/{_APPROVALS_REQUIRED}",
            )
            return True
        saved = _save_correct_crops(meta, int(reactor_user_id or 0), str(reactor_name or ""))
        metadata_count = _write_verified_labels(meta)
        log_action(
            "discord_feedback_correct_finalized",
            f"msg={reply_message_id}; user={reactor_user_id}",
            f"crops={saved}; metadata_labels={metadata_count}",
        )
    else:
        _append_vote(meta, "incorrect", int(reactor_user_id or 0))
        saved = _save_incorrect_record(meta, int(reactor_user_id or 0), str(reactor_name or ""))
        metadata_count = _mark_metadata_incorrect(meta)
        log_action(
            "discord_feedback_incorrect_finalized",
            f"msg={reply_message_id}; user={reactor_user_id}",
            f"items={saved}; metadata_rows={metadata_count}",
        )

    meta["finalized"] = True
    meta["final_emoji"] = symbol
    meta["finalized_at"] = _utc_now_iso()
    meta["finalized_by"] = int(reactor_user_id or 0)
    meta["finalized_count"] = int(saved)
    if metadata_count > 0:
        meta["metadata_sync_count"] = int(metadata_count)
    _dump_json(meta_path, meta)
    return True


def load_verified_gallery_records() -> List[Dict[str, Any]]:
    """Return verified crop records suitable for gallery rebuild inclusion."""
    _ensure_dirs()
    out: List[Dict[str, Any]] = []
    for rec_path in sorted(_CORRECT_RECORDS_DIR.glob("*.json")):
        rec = _load_json(rec_path)
        if not rec:
            continue
        cat_name = str(rec.get("cat_name") or "").strip()
        crop_path = Path(str(rec.get("crop_path") or "").strip())
        if not cat_name or not crop_path.exists():
            continue
        rec_id = str(rec.get("id") or rec_path.stem)
        out.append({
            "id": rec_id,
            "cat_name": cat_name,
            "crop_path": crop_path,
            "source": "discord_correct",
            "created_at": rec.get("created_at"),
        })
    return out
