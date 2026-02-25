"""Persist Discord CV feedback for gallery retrain and sheet pipeline continuity."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image

from ..config import settings
from ..logger import log_action
from .catsheets import force_refresh_tcb_cache
from .sheets_client import sheets_client

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

# TCB Pics Formatted columns (0-based).
_COL_URL = 6
_COL_SERIAL = 7
_COL_BOX_COORDS = 8
_COL_BOX_CAT_IDS = 9
_DEFAULT_COMMENT_COL = 10

_SN_RE = re.compile(r"sn(\d+)", re.IGNORECASE)
_SHEET_LOCK = threading.Lock()
_DIRS_READY = False
_SHEET_WRITE_BACKOFF_SEC = max(300, int(os.getenv("VISION_FEEDBACK_SHEET_BACKOFF_SEC", "21600") or "21600"))
_sheet_writes_disabled_until_mono: float = 0.0
_sheet_disable_reason: str = ""
_sheet_disable_next_log_mono: float = 0.0


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
    pending_dirs = [_PENDING_META_DIR, _LEGACY_PENDING_META_DIR]
    for pending_dir in pending_dirs:
        for meta_path in pending_dir.glob("*.json"):
            meta = _load_json(meta_path)
            ts = float(meta.get("created_ts") or 0.0)
            if ts <= 0 or (now - ts) <= _PENDING_TTL_SEC:
                continue
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                msg_id = int(meta.get("reply_message_id") or int(meta_path.stem))
                _pending_image_path(msg_id).unlink(missing_ok=True)
                _legacy_pending_image_path(msg_id).unlink(missing_ok=True)
            except Exception:
                pass


def _normalize_header_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").strip().lower())


def _find_header_col(headers: List[str], candidates: List[str]) -> int | None:
    wanted = {_normalize_header_token(c) for c in candidates}
    for idx, h in enumerate(headers):
        if _normalize_header_token(h) in wanted:
            return idx
    return None


def _col_to_a1(col_index_1_based: int) -> str:
    n = int(col_index_1_based)
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _is_sheet_cell_limit_error(exc: Exception) -> bool:
    txt = str(exc or "").lower()
    return ("number of cells" in txt and "10000000" in txt) or ("above the limit of 10000000 cells" in txt)


def _sheet_writes_disabled() -> bool:
    return time.monotonic() < float(_sheet_writes_disabled_until_mono)


def _disable_sheet_writes(reason: str) -> None:
    global _sheet_writes_disabled_until_mono, _sheet_disable_reason, _sheet_disable_next_log_mono
    now = time.monotonic()
    _sheet_writes_disabled_until_mono = max(_sheet_writes_disabled_until_mono, now + float(_SHEET_WRITE_BACKOFF_SEC))
    _sheet_disable_reason = str(reason or "").strip()
    if now >= float(_sheet_disable_next_log_mono):
        until_epoch = int(time.time() + max(0.0, _sheet_writes_disabled_until_mono - now))
        log_action("discord_feedback_sheet_sync_disabled", f"until={until_epoch}", _sheet_disable_reason or "sheet capacity/backoff")
        _sheet_disable_next_log_mono = now + 300.0


def _parse_serial(serial_text: str) -> int | None:
    s = str(serial_text or "").strip()
    m = _SN_RE.search(s)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    if s.isdigit():
        return int(s)
    return None


def _normalize_rows(rows: Any) -> List[List[str]]:
    if not isinstance(rows, list):
        return []
    out: List[List[str]] = []
    for row in rows:
        if isinstance(row, list):
            out.append(["" if v is None else str(v) for v in row])
        elif isinstance(row, tuple):
            out.append(["" if v is None else str(v) for v in list(row)])
        else:
            out.append([str(row)])
    return out


def _open_tcb_sheet_with_rows() -> Tuple[Any, List[List[str]], int]:
    gc = sheets_client()
    sh = gc.open_by_key(settings.sheet_catabase_id)
    ws = sh.worksheet("TCB Pics Formatted")
    rows: List[List[str]]
    try:
        rows = _normalize_rows(ws.get_all_values())
    except Exception:
        try:
            raw = sh.values_get("'TCB Pics Formatted'")
            rows = _normalize_rows((raw or {}).get("values") or [])
        except Exception:
            raw = sh.values_get("'TCB Pics Formatted'!A:ZZ")
            rows = _normalize_rows((raw or {}).get("values") or [])
    headers = rows[0] if rows else []
    comment_col = _find_header_col(
        headers,
        [
            "OfficerComments",
            "Officer Comments",
            "Comments",
            "Comment",
        ],
    )
    return ws, rows, int(comment_col if comment_col is not None else _DEFAULT_COMMENT_COL)


def _build_discord_comment(
    existing: str,
    *,
    source_message_id: int,
    reply_message_id: int,
    status: str,
) -> str:
    txt = str(existing or "").strip()
    clean_parts: List[str] = []
    if txt:
        for part in txt.split(";"):
            p = part.strip()
            low = p.lower()
            if low.startswith("discord_source_msg="):
                continue
            if low.startswith("discord_reply_msg="):
                continue
            if low.startswith("discord_status="):
                continue
            if low.startswith("discord_updated="):
                continue
            clean_parts.append(p)
    clean_parts.append(f"discord_source_msg={int(source_message_id)}")
    clean_parts.append(f"discord_reply_msg={int(reply_message_id)}")
    clean_parts.append(f"discord_status={str(status or '').strip().lower() or 'pending'}")
    clean_parts.append(f"discord_updated={_utc_now_iso()}")
    return "; ".join(clean_parts)


def _cache_labeler_image(serial: int, image_bytes: bytes) -> None:
    if serial <= 0 or not image_bytes:
        return
    try:
        cache_dir = Path("cache") / "labeler"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"sn{int(serial):04d}.jpg").write_bytes(image_bytes)
    except Exception:
        pass


def _upsert_sheet_row(
    meta: Dict[str, Any],
    *,
    status: str,
    image_bytes: bytes | None = None,
) -> Tuple[int, int]:
    """Ensure Discord identify image exists in TCB Pics Formatted.

    Returns:
      (serial_number, row_number_1_based)
    """
    source_msg = int(meta.get("source_message_id") or 0)
    reply_msg = int(meta.get("reply_message_id") or 0)
    source_url = str(meta.get("source_image_url") or "").strip()
    source_tag = f"discord_source_msg={source_msg}"
    if _sheet_writes_disabled():
        return 0, 0
    with _SHEET_LOCK:
        try:
            ws, rows, comment_col = _open_tcb_sheet_with_rows()
            headers = rows[0] if rows else []
            matched_row_num = 0
            matched_serial = 0
            existing_comment = ""
            for idx, row in enumerate(rows[1:], start=2):
                comment = row[comment_col] if len(row) > comment_col else ""
                if source_msg > 0 and source_tag and source_tag in str(comment):
                    matched_row_num = int(idx)
                elif source_url and str(row[_COL_URL] if len(row) > _COL_URL else "").strip() == source_url:
                    matched_row_num = int(idx)
                else:
                    continue
                existing_comment = str(comment or "")
                sn = _parse_serial(str(row[_COL_SERIAL] if len(row) > _COL_SERIAL else ""))
                matched_serial = int(sn or 0)
                break

            if matched_row_num <= 0:
                max_sn = 0
                for row in rows[1:]:
                    sn = _parse_serial(str(row[_COL_SERIAL] if len(row) > _COL_SERIAL else ""))
                    if sn:
                        max_sn = max(max_sn, int(sn))
                matched_serial = int(max_sn + 1)
                matched_row_num = int((len(rows) if rows else 0) + 1)

                width = max(_COL_BOX_CAT_IDS + 1, comment_col + 1, len(headers) if headers else 11)
                new_row = [""] * int(width)
                if source_url.startswith("http"):
                    new_row[_COL_URL] = source_url
                new_row[_COL_SERIAL] = f"sn{matched_serial:04d}"
                new_row[comment_col] = _build_discord_comment(
                    "",
                    source_message_id=source_msg,
                    reply_message_id=reply_msg,
                    status=status,
                )
                ws.append_row(new_row, value_input_option="USER_ENTERED")
            else:
                if matched_serial <= 0:
                    max_sn = 0
                    for row in rows[1:]:
                        sn = _parse_serial(str(row[_COL_SERIAL] if len(row) > _COL_SERIAL else ""))
                        if sn:
                            max_sn = max(max_sn, int(sn))
                    matched_serial = int(max_sn + 1)
                merged_comment = _build_discord_comment(
                    existing_comment,
                    source_message_id=source_msg,
                    reply_message_id=reply_msg,
                    status=status,
                )
                updates = [
                    {
                        "range": f"{_col_to_a1(comment_col + 1)}{matched_row_num}",
                        "values": [[merged_comment]],
                    }
                ]
                if matched_serial > 0:
                    updates.append({
                        "range": f"H{matched_row_num}",
                        "values": [[f"sn{matched_serial:04d}"]],
                    })
                ws.batch_update(updates)

            if matched_serial > 0 and image_bytes:
                _cache_labeler_image(matched_serial, image_bytes)

            try:
                force_refresh_tcb_cache()
            except Exception:
                pass
            return int(matched_serial), int(matched_row_num)
        except Exception as e:
            if _is_sheet_cell_limit_error(e):
                _disable_sheet_writes(str(e))
                return 0, 0
            raise


def _build_sheet_labels(meta: Dict[str, Any]) -> Tuple[str, str]:
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


def _write_sheet_verified_labels(meta: Dict[str, Any]) -> int:
    box_coords, box_cat_ids = _build_sheet_labels(meta)
    if not box_coords or not box_cat_ids:
        return 0
    serial, row_num = _upsert_sheet_row(meta, status="verified")
    if serial <= 0 or row_num <= 0:
        return 0
    with _SHEET_LOCK:
        ws, _, _ = _open_tcb_sheet_with_rows()
        ws.batch_update([
            {"range": f"I{row_num}", "values": [[box_coords]]},
            {"range": f"J{row_num}", "values": [[box_cat_ids]]},
        ])
    try:
        force_refresh_tcb_cache()
    except Exception:
        pass
    return len([t for t in box_cat_ids.split("|") if str(t).strip()])


def _mark_sheet_incorrect(meta: Dict[str, Any]) -> int:
    serial, row_num = _upsert_sheet_row(meta, status="incorrect")
    if serial <= 0 or row_num <= 0:
        return 0
    with _SHEET_LOCK:
        ws, _, _ = _open_tcb_sheet_with_rows()
        ws.batch_update([
            {"range": f"I{row_num}", "values": [[""]]},
            {"range": f"J{row_num}", "values": [[""]]},
        ])
    try:
        force_refresh_tcb_cache()
    except Exception:
        pass
    return 1


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
) -> None:
    """Persist identify result context and ensure source image is represented in sheet."""
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
            "image_path": str(img_path),
            "results": clean_results,
            "votes": {"correct": [], "incorrect": []},
            "finalized": False,
            "final_emoji": "",
            "finalized_at": "",
            "finalized_by": 0,
        }
        serial, row_num = _upsert_sheet_row(payload, status="pending", image_bytes=image_bytes)
        if serial > 0:
            payload["sheet_serial"] = int(serial)
        if row_num > 0:
            payload["sheet_row"] = int(row_num)
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
                    "sheet_serial": int(meta.get("sheet_serial") or 0),
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
            "sheet_serial": int(meta.get("sheet_serial") or 0),
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
    sheet_count = 0
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
        sheet_count = _write_sheet_verified_labels(meta)
        log_action(
            "discord_feedback_correct_finalized",
            f"msg={reply_message_id}; user={reactor_user_id}",
            f"crops={saved}; sheet_labels={sheet_count}",
        )
    else:
        _append_vote(meta, "incorrect", int(reactor_user_id or 0))
        saved = _save_incorrect_record(meta, int(reactor_user_id or 0), str(reactor_name or ""))
        sheet_count = _mark_sheet_incorrect(meta)
        log_action(
            "discord_feedback_incorrect_finalized",
            f"msg={reply_message_id}; user={reactor_user_id}",
            f"items={saved}; sheet_rows={sheet_count}",
        )

    meta["finalized"] = True
    meta["final_emoji"] = symbol
    meta["finalized_at"] = _utc_now_iso()
    meta["finalized_by"] = int(reactor_user_id or 0)
    meta["finalized_count"] = int(saved)
    if sheet_count > 0:
        meta["sheet_sync_count"] = int(sheet_count)
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
