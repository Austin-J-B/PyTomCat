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
from typing import Dict, Optional, Set, Tuple

import discord

from ..config import settings
from ..logger import log_action

_SN_RE = re.compile(r"^sn(\d+)$", re.IGNORECASE)
_INDEX_LOCK = threading.Lock()
_INDEX_PATHS: Dict[int, Path] = {}
_INDEX_SERIALS: Set[int] = set()
_INDEX_NEXT_REFRESH_MONO: float = 0.0
_INDEX_REFRESH_SEC = 10.0
_INDEX_ROOT_SIG: Tuple[str, int, int] = ("", 0, 0)
_SERIAL_LOCK = asyncio.Lock()
_NEXT_SERIAL: int | None = None

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
    return bool(settings.channel_sheet_map and channel_id in settings.channel_sheet_map)


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
    needs_header = (not path.exists()) or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


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
