"""Local photo resolver for serial-numbered cat images."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import discord

from ..config import settings
from ..logger import log_action

_SN_RE = re.compile(r"^sn(\d+)$", re.IGNORECASE)
_INDEX_LOCK = threading.Lock()
_METADATA_FILE_LOCK = threading.RLock()
_METADATA_SHEET_SYNC_LOCK = threading.Lock()
_INDEX_PATHS: Dict[int, Path] = {}
_INDEX_SERIALS: Set[int] = set()
_INDEX_NEXT_REFRESH_MONO: float = 0.0
_INDEX_REFRESH_SEC = 10.0
_INDEX_ROOT_SIG: Tuple[str, int, int] = ("", 0, 0)
_SERIAL_LOCK = asyncio.Lock()
_SERIAL_ALLOC_LOCK = threading.RLock()
_NEXT_SERIAL: int | None = None
_LAST_METADATA_SHEET_SYNC_SIG: tuple[int, int] | None = None

CSV_HEADERS = [
    "Discord URL",
    "Timestamp",
    "Author ID",
    "Channel",
    "Guild ID",
    "Message ID",
    "Serial Number",
    "Box Coordinates",
    "Box Cat IDs",
    "Label Author",
    "Comments",
]

_CONTENT_TYPE_EXTS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_SKIP_LABEL_TOKENS = {
    "",
    "rejected",
    "needsreview",
    "needs review",
    "notacat",
    "not a cat",
    "0.notacat",
    "0. notacat",
}


@dataclass(frozen=True)
class IngestResult:
    saved_rows: int
    skipped_rows: int
    saved_serials: tuple[str, ...]


def photo_root() -> Path:
    """Configured local photo root for labeler reads."""
    raw = str(getattr(settings, "labeler_local_photo_root", "") or "").strip()
    return Path(raw or "./cache/PicsOfCats/Pictures")


def is_intake_message(message: discord.Message) -> bool:
    """True when a message should be added to the local photo intake."""
    attachments = getattr(message, "attachments", None) or []
    if not attachments:
        return False
    if getattr(message, "guild", None) is None:
        return True
    channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
    return bool(settings.image_intake_channel_map and channel_id in settings.image_intake_channel_map)


def metadata_csv_path() -> Path:
    """Configured local metadata CSV path."""
    raw = str(getattr(settings, "photo_metadata_csv", "") or "").strip()
    return Path(raw or "./TomCatBot Pics.csv")


def ensure_storage_ready() -> tuple[Path, Path]:
    """Ensure the photo root and metadata CSV exist."""
    root = photo_root()
    root.mkdir(parents=True, exist_ok=True)
    metadata_path = metadata_csv_path()
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    if not metadata_path.exists() or metadata_path.stat().st_size == 0:
        with metadata_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
            writer.writeheader()
    return root, metadata_path


def allowed_exts() -> Tuple[str, ...]:
    """Normalized allowed image extensions."""
    exts = getattr(settings, "labeler_local_allowed_exts", None)
    out = []
    seen = set()
    if isinstance(exts, (list, tuple)):
        for ext in exts:
            e = str(ext or "").strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = f".{e}"
            if e in seen:
                continue
            seen.add(e)
            out.append(e)
    if out:
        return tuple(out)
    return (".jpg", ".jpeg", ".png", ".webp")


def is_local_only() -> bool:
    """True when labeler should not fetch remote URLs."""
    return bool(getattr(settings, "labeler_local_only", True))


def _root_signature(root: Path) -> Tuple[str, int, int]:
    try:
        st = root.stat()
        return (str(root.resolve()), int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return (str(root), 0, 0)


def _scan_index(root: Path, exts: Tuple[str, ...]) -> tuple[Dict[int, Path], Set[int]]:
    paths: Dict[int, Path] = {}
    serials: Set[int] = set()
    if not root.is_dir():
        return paths, serials
    ext_rank = {ext: idx for idx, ext in enumerate(exts)}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix not in ext_rank:
            continue
        m = _SN_RE.match(p.stem)
        if not m:
            continue
        try:
            sn = int(m.group(1))
        except Exception:
            continue
        if sn <= 0:
            continue
        cur = paths.get(sn)
        if cur is None:
            paths[sn] = p
            serials.add(sn)
            continue
        cur_rank = ext_rank.get(cur.suffix.lower(), 999)
        new_rank = ext_rank.get(suffix, 999)
        if new_rank < cur_rank:
            paths[sn] = p
    return paths, serials


def _ensure_index(force: bool = False) -> None:
    global _INDEX_PATHS, _INDEX_SERIALS, _INDEX_NEXT_REFRESH_MONO, _INDEX_ROOT_SIG
    now = time.monotonic()
    root = photo_root()
    sig = _root_signature(root)
    if not force and now < float(_INDEX_NEXT_REFRESH_MONO) and sig == _INDEX_ROOT_SIG:
        return
    with _INDEX_LOCK:
        now = time.monotonic()
        root = photo_root()
        sig = _root_signature(root)
        if not force and now < float(_INDEX_NEXT_REFRESH_MONO) and sig == _INDEX_ROOT_SIG:
            return
        paths, serials = _scan_index(root, allowed_exts())
        _INDEX_PATHS = paths
        _INDEX_SERIALS = serials
        _INDEX_ROOT_SIG = sig
        _INDEX_NEXT_REFRESH_MONO = now + float(_INDEX_REFRESH_SEC)


def has_local_photo(serial: int, *, force_refresh: bool = False) -> bool:
    """True if local bytes are present for serial."""
    try:
        sn = int(serial)
    except Exception:
        return False
    if sn <= 0:
        return False
    _ensure_index(force=force_refresh)
    return sn in _INDEX_SERIALS


def get_local_photo_path(serial: int, *, force_refresh: bool = False) -> Optional[Path]:
    """Resolve local image path for serial, if present."""
    try:
        sn = int(serial)
    except Exception:
        return None
    if sn <= 0:
        return None
    _ensure_index(force=force_refresh)
    p = _INDEX_PATHS.get(sn)
    if p is not None and p.is_file():
        return p
    if force_refresh:
        return None
    _ensure_index(force=True)
    p = _INDEX_PATHS.get(sn)
    if p is not None and p.is_file():
        return p
    return None


def read_local_photo_bytes(serial: int, *, force_refresh: bool = False) -> Optional[bytes]:
    """Read local image bytes for serial."""
    p = get_local_photo_path(serial, force_refresh=force_refresh)
    if p is None:
        return None
    try:
        return p.read_bytes()
    except Exception:
        return None


def local_serials(*, force_refresh: bool = False) -> Set[int]:
    """Return copy of locally available serials."""
    _ensure_index(force=force_refresh)
    return set(_INDEX_SERIALS)


def refresh_local_index() -> None:
    """Force a rescan after new local photos are written."""
    _ensure_index(force=True)


def _format_serial(serial: int) -> str:
    return f"sn{int(serial):04d}"


def _max_serial_from_csv(path: Path) -> int:
    if not path.is_file():
        return 0
    max_serial = 0
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                raw = str((row or {}).get("Serial Number", "") or "").strip()
                if not raw:
                    continue
                match = _SN_RE.match(raw)
                if not match:
                    continue
                try:
                    sn = int(match.group(1))
                except Exception:
                    continue
                if sn > max_serial:
                    max_serial = sn
    except FileNotFoundError:
        return 0
    return max_serial


def _ensure_next_serial_locked(metadata_path: Path) -> int:
    global _NEXT_SERIAL
    if _NEXT_SERIAL is None:
        csv_max = _max_serial_from_csv(metadata_path)
        local_max = max(local_serials(force_refresh=True), default=0)
        _NEXT_SERIAL = max(csv_max, local_max) + 1
    return int(_NEXT_SERIAL)


def _normalize_ext(ext: str) -> str:
    value = str(ext or "").strip().lower()
    if not value:
        return ""
    if not value.startswith("."):
        value = f".{value}"
    if value == ".jpe":
        value = ".jpg"
    return value


def _choose_attachment_ext(attachment: discord.Attachment) -> str:
    allowed = {ext.lower() for ext in allowed_exts()}
    suffix = _normalize_ext(Path(getattr(attachment, "filename", "") or "").suffix)
    if suffix in allowed:
        return suffix

    content_type = str(getattr(attachment, "content_type", "") or "").split(";", 1)[0].strip().lower()
    mapped = _normalize_ext(_CONTENT_TYPE_EXTS.get(content_type, ""))
    if mapped in allowed:
        return mapped

    guessed = _normalize_ext(mimetypes.guess_extension(content_type or ""))
    if guessed == ".jpeg" and ".jpeg" not in allowed and ".jpg" in allowed:
        guessed = ".jpg"
    if guessed in allowed:
        return guessed
    return ""


def _looks_like_image(attachment: discord.Attachment) -> bool:
    content_type = str(getattr(attachment, "content_type", "") or "").strip().lower()
    if content_type.startswith("image/"):
        return True
    suffix = _normalize_ext(Path(getattr(attachment, "filename", "") or "").suffix)
    return suffix in {".jpg", ".jpeg", ".png", ".webp"}


def _preferred_storage_ext(
    *,
    filename: str = "",
    content_type: str = "",
    discord_url: str = "",
) -> str:
    allowed = {ext.lower() for ext in allowed_exts()}
    candidates = [
        _normalize_ext(Path(str(filename or "")).suffix),
        _normalize_ext(_CONTENT_TYPE_EXTS.get(str(content_type or "").split(";", 1)[0].strip().lower(), "")),
        _normalize_ext(Path(str(discord_url or "").split("?", 1)[0]).suffix),
    ]
    guessed = _normalize_ext(mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip().lower()))
    if guessed == ".jpeg" and ".jpeg" not in allowed and ".jpg" in allowed:
        guessed = ".jpg"
    candidates.append(guessed)
    for ext in candidates:
        if ext and ext in allowed:
            return ext
    for fallback in (".jpg", ".jpeg", ".png", ".webp"):
        if fallback in allowed:
            return fallback
    return ".jpg"


def _timestamp_for_message(message: discord.Message) -> str:
    created_at = getattr(message, "created_at", None)
    if created_at is None:
        created_at = dt.datetime.now(dt.timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    else:
        created_at = created_at.astimezone(dt.timezone.utc)
    return created_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_metadata_row(path: Path, row: dict[str, str]) -> None:
    with _METADATA_FILE_LOCK:
        needs_header = (not path.exists()) or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)


def read_metadata_rows() -> list[dict[str, str]]:
    """Return metadata rows as dicts using the canonical CSV header order."""
    _, metadata_path = ensure_storage_ready()
    with _METADATA_FILE_LOCK:
        rows: list[dict[str, str]] = []
        with metadata_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for raw in reader:
                row = {header: str((raw or {}).get(header, "") or "") for header in CSV_HEADERS}
                rows.append(row)
        return rows


def read_metadata_table() -> list[list[str]]:
    """Return the metadata CSV as a full table, including the header row."""
    rows = read_metadata_rows()
    table: list[list[str]] = [list(CSV_HEADERS)]
    for row in rows:
        table.append([str((row or {}).get(header, "") or "") for header in CSV_HEADERS])
    return table


def _refresh_photo_metadata_consumers() -> None:
    """Invalidate cached metadata views after local photo rows change."""
    try:
        from .catsheets import force_refresh_photo_rows_cache

        force_refresh_photo_rows_cache()
    except Exception:
        pass
    try:
        from . import show_cache

        show_cache.reset_photo_metadata_cache()
    except Exception:
        pass


def _find_existing_metadata_row(
    rows: list[dict[str, str]],
    *,
    message_id: str,
    discord_url: str,
) -> tuple[Optional[dict[str, str]], Optional[int]]:
    clean_url = str(discord_url or "").strip()
    clean_msg = str(message_id or "").strip()
    if clean_url:
        for row in rows:
            row_url = str((row or {}).get("Discord URL", "") or "").strip()
            if row_url and row_url == clean_url:
                serial = _parse_serial_like((row or {}).get("Serial Number"))
                return row, serial
    if clean_msg:
        for row in rows:
            row_msg = str((row or {}).get("Message ID", "") or "").strip()
            if row_msg and row_msg == clean_msg:
                serial = _parse_serial_like((row or {}).get("Serial Number"))
                return row, serial
    return None, None


def upsert_photo_bytes(
    image_bytes: bytes,
    *,
    discord_url: str = "",
    timestamp: str = "",
    author_id: str = "",
    channel: str = "",
    guild_id: str = "",
    message_id: str = "",
    filename: str = "",
    content_type: str = "",
) -> dict[str, Any]:
    """Ensure arbitrary image bytes exist in the local photo store and metadata CSV."""
    global _NEXT_SERIAL
    if not image_bytes:
        return {"serial": 0, "created": False, "updated": False, "wrote_file": False}

    root, metadata_path = ensure_storage_ready()
    ext = _preferred_storage_ext(
        filename=str(filename or ""),
        content_type=str(content_type or ""),
        discord_url=str(discord_url or ""),
    )
    clean_values = {
        "Discord URL": str(discord_url or "").strip(),
        "Timestamp": str(timestamp or "").strip(),
        "Author ID": str(author_id or "").strip(),
        "Channel": str(channel or "").strip(),
        "Guild ID": str(guild_id or "").strip(),
        "Message ID": str(message_id or "").strip(),
    }

    wrote_file = False
    metadata_changed = False
    created = False
    photo_path: Optional[Path] = None
    serial = 0
    serial_token = ""

    with _SERIAL_ALLOC_LOCK:
        with _METADATA_FILE_LOCK:
            rows = read_metadata_rows()
            row, serial = _find_existing_metadata_row(
                rows,
                message_id=clean_values["Message ID"],
                discord_url=clean_values["Discord URL"],
            )

            if row is None:
                serial = _ensure_next_serial_locked(metadata_path)
                serial_token = _format_serial(serial)
                collision = next(root.glob(f"{serial_token}.*"), None)
                if collision is not None:
                    raise RuntimeError(f"serial collision for {serial_token}: {collision}")
                photo_path = root / f"{serial_token}{ext}"
                photo_path.write_bytes(image_bytes)
                wrote_file = True
                created = True
                row = {
                    "Discord URL": clean_values["Discord URL"],
                    "Timestamp": clean_values["Timestamp"],
                    "Author ID": clean_values["Author ID"],
                    "Channel": clean_values["Channel"],
                    "Guild ID": clean_values["Guild ID"],
                    "Message ID": clean_values["Message ID"],
                    "Serial Number": serial_token,
                    "Box Coordinates": "",
                    "Box Cat IDs": "",
                    "Label Author": "",
                    "Comments": "",
                }
                _append_metadata_row(metadata_path, row)
                rows.append(row)
                metadata_changed = True
                _NEXT_SERIAL = int(serial) + 1
            else:
                serial = int(serial or 0)
                if serial <= 0:
                    serial = _ensure_next_serial_locked(metadata_path)
                    row["Serial Number"] = _format_serial(serial)
                    metadata_changed = True
                    _NEXT_SERIAL = int(serial) + 1
                serial_token = _format_serial(serial)

                for field, value in clean_values.items():
                    if value and not str((row or {}).get(field, "") or "").strip():
                        row[field] = value
                        metadata_changed = True

                existing_path = get_local_photo_path(serial, force_refresh=True)
                if existing_path is not None:
                    photo_path = existing_path
                else:
                    photo_path = root / f"{serial_token}{ext}"
                    photo_path.write_bytes(image_bytes)
                    wrote_file = True

                if metadata_changed:
                    _write_metadata_rows_locked(metadata_path, rows)

    if wrote_file:
        refresh_local_index()
    if metadata_changed:
        _refresh_photo_metadata_consumers()
    return {
        "serial": int(serial or 0),
        "serial_token": serial_token,
        "created": bool(created),
        "updated": bool(metadata_changed),
        "wrote_file": bool(wrote_file),
        "path": str(photo_path) if photo_path is not None else "",
    }


def _metadata_file_signature(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return int(st.st_mtime_ns), int(st.st_size)
    except Exception:
        return 0, 0


def _sheet_col_name(col_idx: int) -> str:
    """Convert a 1-based column index into A1 notation letters."""
    idx = int(col_idx)
    if idx <= 0:
        return "A"
    letters = []
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


def sync_metadata_csv_to_sheet(force: bool = False) -> dict[str, Any]:
    """Mirror the local TomCatBot Pics CSV into the backup worksheet."""
    if not bool(getattr(settings, "photo_metadata_sheet_sync_enabled", True)):
        return {"status": "disabled"}

    sheet_id = (
        str(getattr(settings, "sheet_catabase_id", "") or "").strip()
        or str(getattr(settings, "cat_spreadsheet_id", "") or "").strip()
    )
    if not sheet_id:
        return {"status": "disabled_no_sheet"}

    _, metadata_path = ensure_storage_ready()
    with _METADATA_SHEET_SYNC_LOCK:
        table = read_metadata_table()
        signature = _metadata_file_signature(metadata_path)
        global _LAST_METADATA_SHEET_SYNC_SIG
        if not force and _LAST_METADATA_SHEET_SYNC_SIG == signature:
            return {
                "status": "skipped",
                "rows": max(0, len(table) - 1),
                "sheet_title": str(getattr(settings, "photo_metadata_sheet_title", "TomCatBot Pics") or "TomCatBot Pics"),
            }

        from gspread.exceptions import WorksheetNotFound  #type: ignore
        from .sheets_client import sheets_client

        sheet_title = str(getattr(settings, "photo_metadata_sheet_title", "TomCatBot Pics") or "TomCatBot Pics").strip() or "TomCatBot Pics"
        chunk_rows = max(50, int(getattr(settings, "photo_metadata_sheet_sync_chunk_rows", 500) or 500))
        spreadsheet = sheets_client().open_by_key(sheet_id)
        try:
            worksheet = spreadsheet.worksheet(sheet_title)
        except WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=sheet_title,
                rows=max(len(table), 1000),
                cols=len(CSV_HEADERS),
            )

        worksheet.clear()
        worksheet.resize(rows=max(len(table), 1), cols=len(CSV_HEADERS))
        last_col = _sheet_col_name(len(CSV_HEADERS))
        for start in range(0, len(table), chunk_rows):
            chunk = table[start:start + chunk_rows]
            if not chunk:
                continue
            row_start = start + 1
            row_end = row_start + len(chunk) - 1
            worksheet.update(
                f"A{row_start}:{last_col}{row_end}",
                chunk,
                value_input_option="RAW",
            )

        _LAST_METADATA_SHEET_SYNC_SIG = signature
        log_action(
            "photo_metadata_sheet_sync",
            f"sheet={sheet_title}",
            f"rows={max(0, len(table) - 1)}; chunk_rows={chunk_rows}",
        )
        return {
            "status": "ok",
            "rows": max(0, len(table) - 1),
            "sheet_title": sheet_title,
        }


async def start_photo_metadata_sheet_sync_scheduler() -> None:
    """Periodically push the local photo metadata CSV into the backup worksheet."""
    if not bool(getattr(settings, "photo_metadata_sheet_sync_enabled", True)):
        return
    interval_sec = max(
        30,
        int(getattr(settings, "photo_metadata_sheet_sync_interval_sec", 300) or 300),
    )
    while True:
        try:
            await asyncio.to_thread(sync_metadata_csv_to_sheet)
        except Exception as e:
            log_action("photo_metadata_sheet_sync_error", "scheduler", str(e))
        await asyncio.sleep(float(interval_sec))


def _write_metadata_rows_locked(path: Path, rows: list[dict[str, str]]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: str((row or {}).get(header, "") or "") for header in CSV_HEADERS})
    tmp_path.replace(path)


def _merge_label_author(existing: str, actor: str) -> str:
    actor_clean = str(actor or "").strip()
    if not actor_clean:
        return str(existing or "").strip()
    names: list[str] = []
    seen: set[str] = set()
    for token in str(existing or "").split(","):
        name = token.strip()
        if not name:
            continue
        marker = name.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        names.append(name)
    if actor_clean.casefold() not in seen:
        names.append(actor_clean)
    return ", ".join(names)


def _has_reviewed_label(box_cat_ids: Any) -> bool:
    labels = [str(v or "").strip() for v in str(box_cat_ids or "").split("|")]
    for label in labels:
        if label.strip().lower() in _SKIP_LABEL_TOKENS:
            continue
        if label:
            return True
    return False


def update_metadata_annotations(updates: list[dict[str, Any]], actor_name: str) -> dict[str, Any]:
    """Apply labeler annotation updates to the metadata CSV."""
    _, metadata_path = ensure_storage_ready()
    with _METADATA_FILE_LOCK:
        rows = read_metadata_rows()
        by_serial: dict[int, dict[str, str]] = {}
        for row in rows:
            raw = str(row.get("Serial Number", "") or "").strip()
            match = _SN_RE.match(Path(raw).stem if "." in raw else raw)
            if not match:
                match = _SN_RE.match(raw)
            if not match:
                continue
            try:
                by_serial[int(match.group(1))] = row
            except Exception:
                continue

        pending_unblacklist_ref_serials: list[int] = []
        saved = 0
        for upd in updates or []:
            serial = _parse_serial_like(upd.get("serial"))
            if serial is None:
                continue
            row = by_serial.get(int(serial))
            if row is None:
                continue
            touched = False
            if "box_coords" in upd:
                row["Box Coordinates"] = str(upd.get("box_coords") or "")
                touched = True
            if "box_cat_ids" in upd:
                row["Box Cat IDs"] = str(upd.get("box_cat_ids") or "")
                if _has_reviewed_label(upd.get("box_cat_ids")):
                    pending_unblacklist_ref_serials.append(int(serial))
                touched = True
            if "comments" in upd:
                row["Comments"] = str(upd.get("comments") or "")
                touched = True
            if touched:
                row["Label Author"] = _merge_label_author(row.get("Label Author", ""), actor_name)
                saved += 1

        _write_metadata_rows_locked(metadata_path, rows)
    if saved > 0:
        _refresh_photo_metadata_consumers()
    return {
        "saved": int(saved),
        "pending_unblacklist_ref_serials": pending_unblacklist_ref_serials,
    }


def clear_metadata_annotations(serials: list[int], actor_name: str) -> dict[str, Any]:
    """Clear label columns for the given serials in the metadata CSV."""
    _, metadata_path = ensure_storage_ready()
    with _METADATA_FILE_LOCK:
        rows = read_metadata_rows()
        by_serial: dict[int, dict[str, str]] = {}
        for row in rows:
            serial = _parse_serial_like(row.get("Serial Number"))
            if serial is not None:
                by_serial[int(serial)] = row

        results: dict[int, dict[str, Any]] = {}
        changed_any = False
        for serial in serials or []:
            sn = _parse_serial_like(serial)
            if sn is None:
                continue
            row = by_serial.get(int(sn))
            if row is None:
                results[int(sn)] = {
                    "ok": False,
                    "not_found": True,
                    "changed": False,
                    "already_unlabeled": False,
                    "error": "Serial not found",
                }
                continue
            prev_box_coords = str(row.get("Box Coordinates", "") or "")
            prev_box_cat_ids = str(row.get("Box Cat IDs", "") or "")
            changed = bool(prev_box_coords.strip() or prev_box_cat_ids.strip())
            row["Box Coordinates"] = ""
            row["Box Cat IDs"] = ""
            row["Label Author"] = _merge_label_author(row.get("Label Author", ""), actor_name)
            changed_any = changed_any or changed
            results[int(sn)] = {
                "ok": True,
                "not_found": False,
                "changed": bool(changed),
                "already_unlabeled": not bool(changed),
            }

        _write_metadata_rows_locked(metadata_path, rows)
    if changed_any:
        _refresh_photo_metadata_consumers()
    return {"results": results, "updated_metadata": bool(changed_any)}


def _parse_serial_like(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    match = _SN_RE.match(Path(text).stem if "." in text else text)
    if not match:
        match = _SN_RE.match(text)
    if not match:
        digits = re.sub(r"\D", "", text)
        if not digits:
            return None
        try:
            parsed = int(digits)
            return parsed if parsed > 0 else None
        except Exception:
            return None
    try:
        parsed = int(match.group(1))
        return parsed if parsed > 0 else None
    except Exception:
        return None


async def ingest_message_images(message: discord.Message) -> IngestResult:
    """Persist supported image attachments locally and append metadata rows."""
    attachments = [a for a in (getattr(message, "attachments", None) or []) if _looks_like_image(a)]
    if not attachments:
        return IngestResult(saved_rows=0, skipped_rows=0, saved_serials=())

    root, metadata_path = ensure_storage_ready()
    timestamp = _timestamp_for_message(message)
    author_id = str(getattr(message.author, "id", "") or "")
    channel_id = str(getattr(message.channel, "id", "") or "")
    guild_id = str(getattr(getattr(message, "guild", None), "id", "") or "")
    message_id = str(getattr(message, "id", "") or "")

    saved_rows = 0
    skipped_rows = 0
    saved_serials: list[str] = []

    for attachment in attachments:
        ext = _choose_attachment_ext(attachment)
        if not ext:
            skipped_rows += 1
            log_action(
                "image_intake_skip",
                f"channel={channel_id or 'dm'}",
                f"unsupported_attachment filename={getattr(attachment, 'filename', '')}; content_type={getattr(attachment, 'content_type', '')}",
            )
            continue

        blob = await attachment.read()
        if not blob:
            skipped_rows += 1
            log_action(
                "image_intake_skip",
                f"channel={channel_id or 'dm'}",
                f"empty_attachment filename={getattr(attachment, 'filename', '')}",
            )
            continue

        async with _SERIAL_LOCK:
            serial = _ensure_next_serial_locked(metadata_path)
            serial_token = _format_serial(serial)
            collision = next(root.glob(f"{serial_token}.*"), None)
            if collision is not None:
                raise RuntimeError(f"serial collision for {serial_token}: {collision}")

            photo_path = root / f"{serial_token}{ext}"
            row = {
                "Discord URL": str(getattr(attachment, "url", "") or ""),
                "Timestamp": timestamp,
                "Author ID": author_id,
                "Channel": channel_id,
                "Guild ID": guild_id,
                "Message ID": message_id,
                "Serial Number": serial_token,
                "Box Coordinates": "",
                "Box Cat IDs": "",
                "Label Author": "",
                "Comments": "",
            }

            try:
                photo_path.write_bytes(blob)
                _append_metadata_row(metadata_path, row)
                saved_rows += 1
                saved_serials.append(serial_token)
                global _NEXT_SERIAL
                _NEXT_SERIAL = serial + 1
            except Exception:
                try:
                    if photo_path.exists():
                        photo_path.unlink()
                except Exception:
                    pass
                raise

    if saved_rows:
        refresh_local_index()
        _refresh_photo_metadata_consumers()
    return IngestResult(saved_rows=saved_rows, skipped_rows=skipped_rows, saved_serials=tuple(saved_serials))


def content_type_for_path(path: Optional[Path]) -> str:
    """Return content-type for a photo path."""
    suffix = str(getattr(path, "suffix", "") or "").strip().lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"
