"""Local photo resolver for serial-numbered cat images."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import math
import mimetypes
import os
import re
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Set, Tuple

import discord
from PIL import Image, ImageOps

from ..config import settings
from ..logger import log_action

_SN_RE = re.compile(r"^sn(\d+)$", re.IGNORECASE)
_INDEX_LOCK = threading.Lock()
_METADATA_FILE_LOCK = threading.RLock()
_METADATA_SHEET_SYNC_LOCK = threading.Lock()
_INDEX_PATHS: Dict[int, Path] = {}
_INDEX_SERIALS: Set[int] = set()
_INDEX_NEXT_REFRESH_MONO: float = 0.0
_INDEX_REFRESH_SEC = max(
    1.0,
    float(getattr(settings, "labeler_local_index_refresh_sec", 60.0) or 60.0),
)
_INDEX_ROOT_SIG: Tuple[str, int, int] = ("", 0, 0)
_INDEX_VERSION: int = 0
_HASH_INDEX_LOCK = threading.Lock()
_HASH_CACHE: Dict[str, tuple[int, int, str, Optional[int], int, int, int]] = {}
_EXACT_HASH_SERIALS: Dict[str, Set[int]] = {}
_DHASH_ENTRIES: Dict[int, Set[int]] = {}
_HASH_INDEX_BUILT_FOR_VERSION: int = -1
_SERIAL_ALLOC_LOCK = threading.RLock()
_NEXT_SERIAL: int | None = None
_LAST_METADATA_SHEET_SYNC_SIG: tuple[int, int] | None = None
_DEDUP_DHASH_MAX_DISTANCE = max(0, int(getattr(settings, "photo_dedupe_dhash_max_distance", 4) or 4))
_DEDUP_ASPECT_RATIO_TOLERANCE = 0.02

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
_CAT_LABEL_PREFIX_RE = re.compile(r"^\s*\d+\s*[.)\-:]?\s*")


@dataclass(frozen=True)
class IngestResult:
    saved_rows: int
    skipped_rows: int
    saved_serials: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateImageMatch:
    serial: int
    kind: str
    distance: int
    path: str


def photo_root() -> Path:
    """Configured local photo root for labeler reads."""
    raw = str(getattr(settings, "labeler_local_photo_root", "") or "").strip()
    return Path(raw or "./cache/PicsOfCats/Pictures")


def intake_target_for_message(message: discord.Message) -> str:
    """Return the configured intake target for a message, if any."""
    attachments = getattr(message, "attachments", None) or []
    if not attachments:
        return ""
    if getattr(message, "guild", None) is None:
        return "photo_metadata"
    channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
    target = settings.image_intake_channel_map.get(channel_id) if settings.image_intake_channel_map else None
    return str(target or "").strip()


def is_intake_message(message: discord.Message) -> bool:
    """True when a message should be added to the local photo intake."""
    return bool(intake_target_for_message(message))


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


def _stable_path_key(path: Path) -> str:
    """Return a stable absolute path without the extra cost of realpath resolution."""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:
        return str(path)


def _root_signature(root: Path) -> Tuple[str, int, int]:
    try:
        st = root.stat()
        return (_stable_path_key(root), int(st.st_mtime_ns), int(st.st_size))
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
    global _INDEX_PATHS, _INDEX_SERIALS, _INDEX_NEXT_REFRESH_MONO, _INDEX_ROOT_SIG, _INDEX_VERSION
    now = time.monotonic()
    # Fast path: within TTL, skip the stat() entirely.  This avoids hundreds
    # of redundant filesystem stat calls when has_local_photo is invoked in a
    # tight loop (e.g. building the manual candidate catalog).
    if not force and now < float(_INDEX_NEXT_REFRESH_MONO):
        return
    root = photo_root()
    sig = _root_signature(root)
    if not force and sig == _INDEX_ROOT_SIG:
        # Sig unchanged — reset TTL and return without acquiring the lock.
        _INDEX_NEXT_REFRESH_MONO = now + float(_INDEX_REFRESH_SEC)
        return
    with _INDEX_LOCK:
        now = time.monotonic()
        root = photo_root()
        sig = _root_signature(root)
        if not force and now < float(_INDEX_NEXT_REFRESH_MONO) and sig == _INDEX_ROOT_SIG:
            return
        paths, serials = _scan_index(root, allowed_exts())
        if force or sig != _INDEX_ROOT_SIG or paths != _INDEX_PATHS or serials != _INDEX_SERIALS:
            _INDEX_VERSION += 1
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
    global _HASH_INDEX_BUILT_FOR_VERSION
    _ensure_index(force=True)
    with _HASH_INDEX_LOCK:
        _HASH_CACHE.clear()
        _EXACT_HASH_SERIALS.clear()
        _DHASH_ENTRIES.clear()
        _HASH_INDEX_BUILT_FOR_VERSION = -1


def _sha256_hex(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def _dhash64(image_bytes: bytes) -> tuple[Optional[int], int, int]:
    if not image_bytes:
        return None, 0, 0
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("L")
            width, height = img.size
            resized = img.resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(resized.tobytes())
    except Exception:
        return None, 0, 0
    value = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            left = pixels[base + col]
            right = pixels[base + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return int(value), int(width), int(height)


def _aspect_ratio_close(width_a: int, height_a: int, width_b: int, height_b: int) -> bool:
    if width_a <= 0 or height_a <= 0 or width_b <= 0 or height_b <= 0:
        return True
    ratio_a = float(width_a) / float(height_a)
    ratio_b = float(width_b) / float(height_b)
    return abs(ratio_a - ratio_b) <= _DEDUP_ASPECT_RATIO_TOLERANCE


def _normalized_storage_bytes(image_bytes: bytes, ext: str) -> bytes:
    max_pixels = int(getattr(settings, "photo_max_pixels", 20_000_000) or 20_000_000)
    if not image_bytes or max_pixels <= 0:
        return image_bytes
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as img:
                img = ImageOps.exif_transpose(img)
                width, height = img.size
                pixels = int(width) * int(height)
                if pixels <= max_pixels:
                    return image_bytes
                scale = math.sqrt(float(max_pixels) / float(max(1, pixels)))
                new_w = max(1, int(width * scale))
                new_h = max(1, int(height * scale))
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                ext_norm = _normalize_ext(ext)
                if ext_norm in {".jpg", ".jpeg"}:
                    if resized.mode not in {"RGB", "L"}:
                        resized = resized.convert("RGB")
                    resized.save(out, format="JPEG", quality=95, optimize=True)
                elif ext_norm == ".png":
                    resized.save(out, format="PNG", optimize=True, compress_level=6)
                elif ext_norm == ".webp":
                    if resized.mode not in {"RGB", "RGBA"}:
                        resized = resized.convert("RGB")
                    resized.save(out, format="WEBP", quality=95, method=6)
                else:
                    if resized.mode not in {"RGB", "L"}:
                        resized = resized.convert("RGB")
                    resized.save(out, format="JPEG", quality=95, optimize=True)
                return out.getvalue()
    except Exception:
        return image_bytes


def _ensure_hash_index(force: bool = False) -> None:
    global _HASH_INDEX_BUILT_FOR_VERSION
    _ensure_index(force=force)
    with _HASH_INDEX_LOCK:
        if not force and _HASH_INDEX_BUILT_FOR_VERSION == _INDEX_VERSION:
            return
        current_paths = dict(_INDEX_PATHS)
        active_path_keys = {_stable_path_key(path) for path in current_paths.values()}
        stale_paths = [path_key for path_key in _HASH_CACHE if path_key not in active_path_keys]
        for path_key in stale_paths:
            _HASH_CACHE.pop(path_key, None)

        exact_hashes: Dict[str, Set[int]] = {}
        dhashes: Dict[int, Set[int]] = {}
        for serial, path in current_paths.items():
            try:
                resolved = _stable_path_key(path)
                stat = path.stat()
            except Exception:
                continue
            cached = _HASH_CACHE.get(resolved)
            if cached and cached[0] == int(stat.st_mtime_ns) and cached[1] == int(stat.st_size):
                _, _, exact_hash, dhash_value, width, height, cached_serial = cached
            else:
                try:
                    image_bytes = path.read_bytes()
                except Exception:
                    continue
                exact_hash = _sha256_hex(image_bytes)
                dhash_value, width, height = _dhash64(image_bytes)
                cached_serial = int(serial)
                _HASH_CACHE[resolved] = (
                    int(stat.st_mtime_ns),
                    int(stat.st_size),
                    exact_hash,
                    dhash_value,
                    int(width),
                    int(height),
                    cached_serial,
                )
            exact_hashes.setdefault(exact_hash, set()).add(int(cached_serial))
            if dhash_value is not None:
                dhashes.setdefault(int(dhash_value), set()).add(int(cached_serial))

        _EXACT_HASH_SERIALS.clear()
        _EXACT_HASH_SERIALS.update(exact_hashes)
        _DHASH_ENTRIES.clear()
        _DHASH_ENTRIES.update(dhashes)
        _HASH_INDEX_BUILT_FOR_VERSION = _INDEX_VERSION


def _path_for_serial(serial: int) -> str:
    path = get_local_photo_path(int(serial), force_refresh=False)
    return str(path) if path is not None else ""


def _find_duplicate_photo(image_bytes: bytes) -> Optional[DuplicateImageMatch]:
    if not image_bytes:
        return None
    exact_hash = _sha256_hex(image_bytes)
    dhash_value, width, height = _dhash64(image_bytes)
    _ensure_hash_index(force=False)
    with _HASH_INDEX_LOCK:
        exact_serials = sorted(_EXACT_HASH_SERIALS.get(exact_hash) or [])
        if exact_serials:
            serial = int(exact_serials[0])
            return DuplicateImageMatch(
                serial=serial,
                kind="exact",
                distance=0,
                path=_path_for_serial(serial),
            )
        if dhash_value is None or _DEDUP_DHASH_MAX_DISTANCE <= 0:
            return None
        best_match: Optional[DuplicateImageMatch] = None
        for existing_hash, serials in _DHASH_ENTRIES.items():
            distance = int((int(existing_hash) ^ int(dhash_value)).bit_count())
            if distance > _DEDUP_DHASH_MAX_DISTANCE:
                continue
            serial_candidates = sorted(int(s) for s in (serials or []) if int(s) > 0)
            if not serial_candidates:
                continue
            serial = int(serial_candidates[0])
            cached_path = get_local_photo_path(serial, force_refresh=False)
            cached_key = _stable_path_key(cached_path) if cached_path is not None else ""
            cached = _HASH_CACHE.get(cached_key) if cached_key else None
            existing_width = int(cached[4]) if cached else 0
            existing_height = int(cached[5]) if cached else 0
            if not _aspect_ratio_close(width, height, existing_width, existing_height):
                continue
            match = DuplicateImageMatch(
                serial=serial,
                kind="near",
                distance=distance,
                path=str(cached_path) if cached_path is not None else "",
            )
            if best_match is None or match.distance < best_match.distance or (
                match.distance == best_match.distance and match.serial < best_match.serial
            ):
                best_match = match
        return best_match


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
        _append_metadata_row_locked(path, row)


def _append_metadata_row_locked(path: Path, row: dict[str, str]) -> None:
    """Append one metadata row while the caller already holds _METADATA_FILE_LOCK."""
    needs_header = (not path.exists()) or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


def _read_metadata_rows_locked(metadata_path: Path) -> list[dict[str, str]]:
    """Read metadata rows while the caller already holds _METADATA_FILE_LOCK."""
    rows: list[dict[str, str]] = []
    with metadata_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = {header: str((raw or {}).get(header, "") or "") for header in CSV_HEADERS}
            rows.append(row)
    return rows


def _build_metadata_row(
    *,
    discord_url: str,
    timestamp: str,
    author_id: str,
    channel: str,
    guild_id: str,
    message_id: str,
    serial_token: str,
) -> dict[str, str]:
    return {
        "Discord URL": str(discord_url or "").strip(),
        "Timestamp": str(timestamp or "").strip(),
        "Author ID": str(author_id or "").strip(),
        "Channel": str(channel or "").strip(),
        "Guild ID": str(guild_id or "").strip(),
        "Message ID": str(message_id or "").strip(),
        "Serial Number": str(serial_token or "").strip(),
        "Box Coordinates": "",
        "Box Cat IDs": "",
        "Label Author": "",
        "Comments": "",
    }


def _store_ingested_attachment(
    *,
    root: Path,
    metadata_path: Path,
    ext: str,
    blob: bytes,
    discord_url: str,
    timestamp: str,
    author_id: str,
    channel: str,
    guild_id: str,
    message_id: str,
) -> str:
    """Persist one newly ingested attachment using the shared serial allocator lock."""
    global _NEXT_SERIAL
    with _SERIAL_ALLOC_LOCK:
        with _METADATA_FILE_LOCK:
            serial = _ensure_next_serial_locked(metadata_path)
            serial_token = _format_serial(serial)
            collision = next(root.glob(f"{serial_token}.*"), None)
            if collision is not None:
                raise RuntimeError(f"serial collision for {serial_token}: {collision}")
            photo_path = root / f"{serial_token}{ext}"
            row = _build_metadata_row(
                discord_url=discord_url,
                timestamp=timestamp,
                author_id=author_id,
                channel=channel,
                guild_id=guild_id,
                message_id=message_id,
                serial_token=serial_token,
            )
            try:
                photo_path.write_bytes(blob)
                _append_metadata_row_locked(metadata_path, row)
                _NEXT_SERIAL = serial + 1
                return serial_token
            except Exception:
                try:
                    if photo_path.exists():
                        photo_path.unlink()
                except Exception:
                    pass
                raise


def read_metadata_rows() -> list[dict[str, str]]:
    """Return metadata rows as dicts using the canonical CSV header order."""
    _, metadata_path = ensure_storage_ready()
    with _METADATA_FILE_LOCK:
        return _read_metadata_rows_locked(metadata_path)


def read_metadata_table() -> list[list[str]]:
    """Return the metadata CSV as a full table, including the header row."""
    rows = read_metadata_rows()
    table: list[list[str]] = [list(CSV_HEADERS)]
    for row in rows:
        table.append([str((row or {}).get(header, "") or "") for header in CSV_HEADERS])
    return table


def _parse_metadata_timestamp(value: str) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _display_cat_label(value: Any) -> str:
    """Normalize a cat label to display text without numeric prefixes."""
    text = str(value or "").strip()
    if not text:
        return ""
    return _CAT_LABEL_PREFIX_RE.sub("", text, count=1).strip()


def _norm_cat_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _display_cat_label(value).lower())


def rename_metadata_cat_labels(rename_map: Mapping[str, str]) -> dict[str, Any]:
    """Rewrite Box Cat IDs when CatDatabase renames an existing cat ID."""
    clean_pairs: dict[str, str] = {}
    pair_descriptions: list[str] = []
    for raw_old, raw_new in (rename_map or {}).items():
        old_name = _display_cat_label(raw_old)
        new_name = _display_cat_label(raw_new)
        old_key = _norm_cat_label(old_name)
        if not old_key or not new_name or old_name == new_name:
            continue
        clean_pairs[old_key] = new_name
        pair_descriptions.append(f"{old_name}->{new_name}")
    if not clean_pairs:
        return {
            "status": "no_renames",
            "rename_pairs": 0,
            "rows_changed": 0,
            "labels_replaced": 0,
        }

    _, metadata_path = ensure_storage_ready()
    rows_changed = 0
    labels_replaced = 0

    with _METADATA_FILE_LOCK:
        rows = _read_metadata_rows_locked(metadata_path)
        for row in rows:
            raw_labels = str((row or {}).get("Box Cat IDs", "") or "")
            if not raw_labels:
                continue
            tokens = [str(token or "").strip() for token in raw_labels.split("|")]
            updated_tokens: list[str] = []
            changed = False
            for token in tokens:
                replacement = clean_pairs.get(_norm_cat_label(token))
                if replacement:
                    updated_tokens.append(replacement)
                    if replacement != token:
                        changed = True
                        labels_replaced += 1
                else:
                    updated_tokens.append(token)
            if not changed:
                continue
            row["Box Cat IDs"] = "|".join(updated_tokens)
            rows_changed += 1

        if rows_changed > 0:
            _write_metadata_rows_locked(metadata_path, rows)

    if rows_changed > 0:
        _refresh_photo_metadata_consumers()
        log_action(
            "catabase_name_sync",
            f"rows={rows_changed}; labels={labels_replaced}",
            ",".join(pair_descriptions[:8]),
        )
        return {
            "status": "ok",
            "rename_pairs": len(clean_pairs),
            "rows_changed": rows_changed,
            "labels_replaced": labels_replaced,
        }
    return {
        "status": "no_matches",
        "rename_pairs": len(clean_pairs),
        "rows_changed": 0,
        "labels_replaced": 0,
    }


def _missing_photo_sync_limit() -> int:
    try:
        return max(0, int(getattr(settings, "photo_sync_missing_max_rows", 200) or 200))
    except Exception:
        return 200


def _missing_photo_sync_recent_days() -> int:
    try:
        return max(0, int(getattr(settings, "photo_sync_missing_recent_days", 90) or 90))
    except Exception:
        return 90


def missing_local_photo_rows(*, limit: int = 0, recent_days: int = 0) -> list[dict[str, str]]:
    """Return metadata rows whose serial exists in CSV but not on local disk."""
    existing = local_serials(force_refresh=True)
    threshold: Optional[dt.datetime] = None
    if int(recent_days or 0) > 0:
        threshold = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(recent_days))

    ranked: list[tuple[dt.datetime, int, dict[str, str]]] = []
    for row in read_metadata_rows():
        serial = _parse_serial_like((row or {}).get("Serial Number"))
        if serial is None or serial <= 0 or int(serial) in existing:
            continue
        timestamp = _parse_metadata_timestamp((row or {}).get("Timestamp", ""))
        if threshold is not None:
            if timestamp is None or timestamp < threshold:
                continue
        ranked.append((
            timestamp or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            int(serial),
            dict(row),
        ))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    rows = [row for _, _, row in ranked]
    if int(limit or 0) > 0:
        return rows[: int(limit)]
    return rows


def _discord_url_identity(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    return text.split("?", 1)[0].rstrip("/").casefold()


def _match_attachment_for_row(
    attachments: list[discord.Attachment],
    row: dict[str, str],
) -> Optional[discord.Attachment]:
    image_attachments = [a for a in (attachments or []) if _looks_like_image(a)]
    if not image_attachments:
        return None

    row_url_key = _discord_url_identity((row or {}).get("Discord URL", ""))
    if row_url_key:
        for attachment in image_attachments:
            candidates = [
                str(getattr(attachment, "url", "") or ""),
                str(getattr(attachment, "proxy_url", "") or ""),
            ]
            if any(_discord_url_identity(candidate) == row_url_key for candidate in candidates):
                return attachment

        row_name = Path(row_url_key).name.casefold()
        if row_name:
            for attachment in image_attachments:
                filename = Path(str(getattr(attachment, "filename", "") or "")).name.casefold()
                if filename and filename == row_name:
                    return attachment

    if len(image_attachments) == 1:
        return image_attachments[0]
    return None


async def sync_missing_local_photos(
    client: discord.Client,
    *,
    limit: Optional[int] = None,
    recent_days: Optional[int] = None,
    use_csv_url_fallback: Optional[bool] = None,
) -> dict[str, Any]:
    """Restore recent missing local photos using metadata rows plus Discord fetches."""
    enabled = bool(getattr(settings, "photo_sync_missing_on_boot", True))
    if not enabled:
        return {"status": "disabled"}

    limit_value = _missing_photo_sync_limit() if limit is None else max(0, int(limit))
    recent_days_value = _missing_photo_sync_recent_days() if recent_days is None else max(0, int(recent_days))
    use_csv_fallback = (
        bool(getattr(settings, "photo_sync_missing_use_csv_url_fallback", True))
        if use_csv_url_fallback is None
        else bool(use_csv_url_fallback)
    )

    candidates = await asyncio.to_thread(
        missing_local_photo_rows,
        limit=limit_value,
        recent_days=recent_days_value,
    )
    result: dict[str, Any] = {
        "status": "ok",
        "requested": int(len(candidates)),
        "restored": 0,
        "restored_from_message": 0,
        "restored_from_csv_url": 0,
        "already_present": 0,
        "message_fetch_failures": 0,
        "attachment_misses": 0,
        "csv_url_failures": 0,
        "errors": 0,
        "recent_days": int(recent_days_value),
        "limit": int(limit_value),
        "sample_failures": [],
    }
    if not candidates:
        result["status"] = "no_candidates"
        return result

    channel_cache: dict[int, Any] = {}
    message_cache: dict[tuple[int, int], Optional[discord.Message]] = {}
    session = None
    if use_csv_fallback:
        import aiohttp

        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    try:
        for row in candidates:
            serial = _parse_serial_like((row or {}).get("Serial Number"))
            if serial is None or serial <= 0:
                result["errors"] += 1
                continue
            if await asyncio.to_thread(has_local_photo, int(serial), force_refresh=False):
                result["already_present"] += 1
                continue

            image_bytes: bytes = b""
            filename = ""
            content_type = ""
            source = ""

            channel_id = _parse_serial_like((row or {}).get("Channel"))
            message_id = _parse_serial_like((row or {}).get("Message ID"))
            if channel_id is not None and message_id is not None:
                cache_key = (int(channel_id), int(message_id))
                message = message_cache.get(cache_key)
                if cache_key not in message_cache:
                    channel = channel_cache.get(int(channel_id))
                    if channel is None:
                        channel = client.get_channel(int(channel_id))
                        if channel is None:
                            try:
                                channel = await client.fetch_channel(int(channel_id))
                            except Exception:
                                channel = None
                        channel_cache[int(channel_id)] = channel

                    if channel is not None and hasattr(channel, "fetch_message"):
                        try:
                            message = await channel.fetch_message(int(message_id))
                        except Exception:
                            message = None
                    else:
                        message = None
                    message_cache[cache_key] = message

                if message is None:
                    result["message_fetch_failures"] += 1
                else:
                    attachment = _match_attachment_for_row(
                        list(getattr(message, "attachments", None) or []),
                        row,
                    )
                    if attachment is None:
                        result["attachment_misses"] += 1
                    else:
                        try:
                            image_bytes = await attachment.read()
                            filename = str(getattr(attachment, "filename", "") or "")
                            content_type = str(getattr(attachment, "content_type", "") or "")
                            source = "message"
                        except Exception:
                            image_bytes = b""

            if not image_bytes and session is not None:
                url = str((row or {}).get("Discord URL", "") or "").strip()
                if url:
                    try:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                image_bytes = await resp.read()
                                content_type = str(resp.headers.get("Content-Type", "") or "")
                                filename = Path(_discord_url_identity(url)).name
                                source = "csv_url"
                            else:
                                result["csv_url_failures"] += 1
                    except Exception:
                        result["csv_url_failures"] += 1

            if not image_bytes:
                if len(result["sample_failures"]) < 8:
                    result["sample_failures"].append(str((row or {}).get("Serial Number", "") or serial))
                continue

            try:
                await asyncio.to_thread(
                    upsert_photo_bytes,
                    image_bytes,
                    discord_url=str((row or {}).get("Discord URL", "") or ""),
                    timestamp=str((row or {}).get("Timestamp", "") or ""),
                    author_id=str((row or {}).get("Author ID", "") or ""),
                    channel=str((row or {}).get("Channel", "") or ""),
                    guild_id=str((row or {}).get("Guild ID", "") or ""),
                    message_id=str((row or {}).get("Message ID", "") or ""),
                    filename=filename,
                    content_type=content_type,
                )
            except Exception:
                result["errors"] += 1
                if len(result["sample_failures"]) < 8:
                    result["sample_failures"].append(str((row or {}).get("Serial Number", "") or serial))
                continue

            if await asyncio.to_thread(has_local_photo, int(serial), force_refresh=True):
                result["restored"] += 1
                if source == "csv_url":
                    result["restored_from_csv_url"] += 1
                else:
                    result["restored_from_message"] += 1
            else:
                result["errors"] += 1
                if len(result["sample_failures"]) < 8:
                    result["sample_failures"].append(str((row or {}).get("Serial Number", "") or serial))
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass

    log_action(
        "photo_sync_missing",
        "startup",
        (
            f"requested={result['requested']}; restored={result['restored']}; "
            f"message={result['restored_from_message']}; csv_url={result['restored_from_csv_url']}; "
            f"message_fetch_failures={result['message_fetch_failures']}; "
            f"attachment_misses={result['attachment_misses']}; "
            f"csv_url_failures={result['csv_url_failures']}; errors={result['errors']}"
        ),
    )
    return result


def _refresh_photo_metadata_consumers() -> None:
    """Invalidate cached metadata views after local photo rows change."""
    try:
        from .catsheets import force_refresh_photo_rows_cache

        force_refresh_photo_rows_cache()
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
    image_bytes = _normalized_storage_bytes(image_bytes, ext)
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
            rows = _read_metadata_rows_locked(metadata_path)
            row, serial = _find_existing_metadata_row(
                rows,
                message_id=clean_values["Message ID"],
                discord_url=clean_values["Discord URL"],
            )

            if row is None:
                duplicate = _find_duplicate_photo(image_bytes)
                if duplicate is not None:
                    log_action(
                        "image_duplicate_skip",
                        f"source={clean_values['Channel'] or 'unknown'}",
                        (
                            f"kind={duplicate.kind}; distance={duplicate.distance}; "
                            f"serial={_format_serial(duplicate.serial)}; "
                            f"message_id={clean_values['Message ID'] or 'n/a'}"
                        ),
                    )
                    return {
                        "serial": int(duplicate.serial),
                        "serial_token": _format_serial(duplicate.serial),
                        "created": False,
                        "updated": False,
                        "wrote_file": False,
                        "path": duplicate.path,
                        "duplicate": True,
                        "duplicate_kind": duplicate.kind,
                        "duplicate_distance": int(duplicate.distance),
                    }
                serial = _ensure_next_serial_locked(metadata_path)
                serial_token = _format_serial(serial)
                collision = next(root.glob(f"{serial_token}.*"), None)
                if collision is not None:
                    raise RuntimeError(f"serial collision for {serial_token}: {collision}")
                photo_path = root / f"{serial_token}{ext}"
                photo_path.write_bytes(image_bytes)
                wrote_file = True
                created = True
                row = _build_metadata_row(
                    discord_url=clean_values["Discord URL"],
                    timestamp=clean_values["Timestamp"],
                    author_id=clean_values["Author ID"],
                    channel=clean_values["Channel"],
                    guild_id=clean_values["Guild ID"],
                    message_id=clean_values["Message ID"],
                    serial_token=serial_token,
                )
                _append_metadata_row_locked(metadata_path, row)
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
        "duplicate": False,
        "duplicate_kind": "",
        "duplicate_distance": 0,
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


def _duplicate_review_labels(box_cat_ids: Any) -> list[str]:
    duplicates: list[str] = []
    seen: set[str] = set()
    display_by_key: dict[str, str] = {}
    for raw in str(box_cat_ids or "").split("|"):
        label = str(raw or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in _SKIP_LABEL_TOKENS:
            continue
        display = display_by_key.get(key) or label
        display_by_key[key] = display
        if key in seen:
            if display not in duplicates:
                duplicates.append(display)
            continue
        seen.add(key)
    return duplicates


def update_metadata_annotations(updates: list[dict[str, Any]], actor_name: str) -> dict[str, Any]:
    """Apply labeler annotation updates to the metadata CSV via fast streaming rewrite."""
    _, metadata_path = ensure_storage_ready()
    
    update_map: dict[int, dict[str, Any]] = {}
    for upd in updates or []:
        serial = _parse_serial_like(upd.get("serial"))
        if serial is not None:
            update_map[int(serial)] = upd

    if not update_map:
        return {"saved": 0, "pending_unblacklist_ref_serials": []}

    pending_unblacklist_ref_serials: list[int] = []
    saved = 0

    with _METADATA_FILE_LOCK:
        tmp_path = metadata_path.with_name(f"{metadata_path.name}.tmp")
        with metadata_path.open("r", newline="", encoding="utf-8-sig") as f_in, \
             tmp_path.open("w", newline="", encoding="utf-8") as f_out:
            
            reader = csv.DictReader(f_in)
            # Ensure headers match exact CSV_HEADERS, with default fallbacks if missing
            out_headers = list(CSV_HEADERS)
            writer = csv.DictWriter(f_out, fieldnames=out_headers)
            writer.writeheader()

            for raw_row in reader:
                row = {h: str((raw_row).get(h, "") or "") for h in out_headers}
                
                raw_sn = str(row.get("Serial Number", "") or "").strip()
                match = _SN_RE.match(Path(raw_sn).stem if "." in raw_sn else raw_sn)
                if not match:
                    match = _SN_RE.match(raw_sn)
                
                if match:
                    try:
                        sn = int(match.group(1))
                        upd = update_map.get(sn)
                        if upd is not None:
                            touched = False
                            if "box_coords" in upd:
                                row["Box Coordinates"] = str(upd.get("box_coords") or "")
                                touched = True
                            if "box_cat_ids" in upd:
                                duplicates = _duplicate_review_labels(upd.get("box_cat_ids"))
                                if duplicates:
                                    tmp_path.unlink(missing_ok=True)
                                    raise ValueError(
                                        "Duplicate cat labels are not allowed in one image: "
                                        + ", ".join(duplicates)
                                    )
                                row["Box Cat IDs"] = str(upd.get("box_cat_ids") or "")
                                if _has_reviewed_label(upd.get("box_cat_ids")):
                                    pending_unblacklist_ref_serials.append(sn)
                                touched = True
                            if "comments" in upd:
                                row["Comments"] = str(upd.get("comments") or "")
                                touched = True
                            if touched:
                                row["Label Author"] = _merge_label_author(row.get("Label Author", ""), actor_name)
                                saved += 1
                    except Exception:
                        pass
                
                writer.writerow(row)
                
        tmp_path.replace(metadata_path)

    if saved > 0:
        _refresh_photo_metadata_consumers()
    return {
        "saved": int(saved),
        "pending_unblacklist_ref_serials": pending_unblacklist_ref_serials,
    }

def clear_metadata_annotations(serials: list[int], actor_name: str) -> dict[str, Any]:
    """Clear label columns for the given serials in the metadata CSV via fast streaming rewrite."""
    _, metadata_path = ensure_storage_ready()
    
    target_sns = set()
    for s in serials or []:
        sn = _parse_serial_like(s)
        if sn is not None:
            target_sns.add(int(sn))
            
    if not target_sns:
        return {"results": {}, "updated_metadata": False}

    results: dict[int, dict[str, Any]] = {}
    for sn in target_sns:
        results[sn] = {
            "ok": False,
            "not_found": True,
            "changed": False,
            "already_unlabeled": False,
            "error": "Serial not found",
        }

    changed_any = False

    with _METADATA_FILE_LOCK:
        tmp_path = metadata_path.with_name(f"{metadata_path.name}.tmp")
        with metadata_path.open("r", newline="", encoding="utf-8-sig") as f_in, \
             tmp_path.open("w", newline="", encoding="utf-8") as f_out:
            
            reader = csv.DictReader(f_in)
            out_headers = list(CSV_HEADERS)
            writer = csv.DictWriter(f_out, fieldnames=out_headers)
            writer.writeheader()

            for raw_row in reader:
                row = {h: str((raw_row).get(h, "") or "") for h in out_headers}
                
                raw_sn = str(row.get("Serial Number", "") or "").strip()
                match = _SN_RE.match(Path(raw_sn).stem if "." in raw_sn else raw_sn)
                if not match:
                    match = _SN_RE.match(raw_sn)
                
                if match:
                    try:
                        sn = int(match.group(1))
                        if sn in target_sns:
                            prev_box_coords = str(row.get("Box Coordinates", "") or "")
                            prev_box_cat_ids = str(row.get("Box Cat IDs", "") or "")
                            changed = bool(prev_box_coords.strip() or prev_box_cat_ids.strip())
                            
                            row["Box Coordinates"] = ""
                            row["Box Cat IDs"] = ""
                            row["Label Author"] = _merge_label_author(row.get("Label Author", ""), actor_name)
                            changed_any = changed_any or changed
                            
                            results[sn] = {
                                "ok": True,
                                "not_found": False,
                                "changed": bool(changed),
                                "already_unlabeled": not bool(changed),
                            }
                    except Exception:
                        pass
                
                writer.writerow(row)
                
        tmp_path.replace(metadata_path)

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
        blob = await asyncio.to_thread(_normalized_storage_bytes, blob, ext)

        duplicate = await asyncio.to_thread(_find_duplicate_photo, blob)
        if duplicate is not None:
            skipped_rows += 1
            log_action(
                "image_intake_skip",
                f"channel={channel_id or 'dm'}",
                (
                    f"duplicate kind={duplicate.kind}; distance={duplicate.distance}; "
                    f"serial={_format_serial(duplicate.serial)}; "
                    f"filename={getattr(attachment, 'filename', '')}"
                ),
            )
            continue

        serial_token = await asyncio.to_thread(
            _store_ingested_attachment,
            root=root,
            metadata_path=metadata_path,
            ext=ext,
            blob=blob,
            discord_url=str(getattr(attachment, "url", "") or ""),
            timestamp=timestamp,
            author_id=author_id,
            channel=channel_id,
            guild_id=guild_id,
            message_id=message_id,
        )
        saved_rows += 1
        saved_serials.append(serial_token)
        await asyncio.to_thread(refresh_local_index)

    if saved_rows:
        await asyncio.to_thread(_refresh_photo_metadata_consumers)
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
