"""Labeler image cache backed only by local photo bytes."""

from __future__ import annotations

import asyncio
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from ..logger import log_action
from . import local_photos

# Cache config
LABELER_CACHE_DIR = Path("cache") / "labeler"
CACHE_SIZE = getattr(settings, "labeler_cache_size", 15)
_CACHE_MAX_FILES = max(CACHE_SIZE, int(os.getenv("LABELER_CACHE_MAX_FILES", "300") or "300"))
_CACHE_MAX_BYTES = max(
    0,
    int(float(os.getenv("LABELER_CACHE_MAX_BYTES", "0") or "0")),
)
if _CACHE_MAX_BYTES <= 0:
    _CACHE_MAX_BYTES = int(
        max(1.0, float(os.getenv("LABELER_BOOT_WARM_BUDGET_GB", "12") or "12")) * 1024 * 1024 * 1024
    )
_DEFAULT_ESTIMATED_IMAGE_BYTES = max(
    150_000,
    int(os.getenv("LABELER_CACHE_ESTIMATED_IMAGE_BYTES", "1600000") or "1600000"),
)

# Thread pool for disk I/O so cache reads/writes never block the event loop.
_DISK_IO_WORKERS = max(2, int(os.getenv("LABELER_CACHE_DISK_IO_WORKERS", "4") or "4"))
_disk_io_pool = ThreadPoolExecutor(
    max_workers=_DISK_IO_WORKERS,
    thread_name_prefix="labeler_disk_io",
)

# Keep the old env fallback for concurrency so existing config stays valid.
_CACHE_FILL_CONCURRENCY = max(
    1,
    int(
        os.getenv(
            "LABELER_CACHE_FILL_CONCURRENCY",
            os.getenv("LABELER_CACHE_DOWNLOAD_CONCURRENCY", "4"),
        )
        or "4"
    ),
)

# In-memory tracking of what's currently cached.
_cached_serials: set[int] = set()
_fill_lock = asyncio.Lock()
_fill_inflight_lock = asyncio.Lock()
_fill_inflight: Dict[int, asyncio.Task] = {}
_active_fills: int = 0
_total_fills_started: int = 0


def get_download_stats() -> str:
    """Return cache-fill diagnostics in the legacy download-stats format."""
    return (
        f"active={_active_fills}/{_CACHE_FILL_CONCURRENCY}; "
        f"inflight_tasks={len(_fill_inflight)}; "
        f"total_started={_total_fills_started}"
    )


def _cache_path(serial: int) -> Path:
    """Path to cached image file."""
    return LABELER_CACHE_DIR / f"sn{str(serial).zfill(4)}.jpg"


def _scan_cache() -> set[int]:
    """Scan cache dir and return set of cached serials."""
    if not LABELER_CACHE_DIR.is_dir():
        return set()
    serials = set()
    for p in LABELER_CACHE_DIR.iterdir():
        if p.suffix.lower() == ".jpg" and p.stem.startswith("sn"):
            try:
                serials.add(int(p.stem[2:]))
            except ValueError:
                pass
    return serials


try:
    _cached_serials = _scan_cache()
except Exception:
    _cached_serials = set()


def _evict_if_needed(max_files: int, keep_serials: set[int], max_bytes: int = 0) -> int:
    """Evict oldest cached files until count/bytes are within limits."""
    try:
        if not LABELER_CACHE_DIR.is_dir():
            return 0
        entries = []
        for p in LABELER_CACHE_DIR.iterdir():
            if p.suffix.lower() != ".jpg" or not p.stem.startswith("sn"):
                continue
            try:
                sn = int(p.stem[2:])
            except Exception:
                continue
            try:
                mt = p.stat().st_mtime
            except Exception:
                mt = 0.0
            try:
                sz = p.stat().st_size
            except Exception:
                sz = 0
            entries.append((sn, mt, int(sz), p))
        total_bytes = sum(int(e[2]) for e in entries)
        over_files = max(0, len(entries) - int(max_files))
        over_bytes = max(0, int(total_bytes) - max(0, int(max_bytes)))
        if over_files <= 0 and over_bytes <= 0:
            return 0

        non_keep = sorted([e for e in entries if e[0] not in keep_serials], key=lambda t: t[1])
        keep = sorted([e for e in entries if e[0] in keep_serials], key=lambda t: t[1])
        ordered = non_keep + keep
        removed = 0
        remaining_files = len(entries)
        remaining_bytes = int(total_bytes)
        for sn, _, sz, p in ordered:
            if remaining_files <= int(max_files) and (max_bytes <= 0 or remaining_bytes <= int(max_bytes)):
                break
            try:
                p.unlink(missing_ok=True)
                _cached_serials.discard(sn)
                removed += 1
                remaining_files -= 1
                remaining_bytes = max(0, remaining_bytes - int(sz))
            except Exception:
                pass
        return removed
    except Exception:
        return 0


def clear_cache() -> int:
    """Clear all cached images. Returns count removed."""
    global _cached_serials
    count = 0
    if LABELER_CACHE_DIR.is_dir():
        for p in LABELER_CACHE_DIR.iterdir():
            try:
                p.unlink()
                count += 1
            except Exception:
                pass
    _cached_serials.clear()
    return count


def get_cached_image(serial: int) -> Optional[bytes]:
    """Return cached image bytes, or None if not cached. Safe to call from threads."""
    p = _cache_path(serial)
    if p.is_file():
        try:
            return p.read_bytes()
        except Exception:
            return None
    return None


async def get_cached_image_async(serial: int) -> Optional[bytes]:
    """Non-blocking version of get_cached_image for async callers."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_disk_io_pool, get_cached_image, serial)


def has_cached_image(serial: int) -> bool:
    """Return True if cached bytes exist on disk for this serial."""
    try:
        return _cache_path(int(serial)).is_file()
    except Exception:
        return False


async def has_cached_image_async(serial: int) -> bool:
    """Non-blocking version of has_cached_image for async callers."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_disk_io_pool, has_cached_image, serial)


def _write_cached_image(serial: int, data: bytes) -> bool:
    """Write image bytes to cache file. Safe to call from threads."""
    try:
        LABELER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(serial).write_bytes(data)
        return True
    except Exception:
        return False


async def _write_cached_image_async(serial: int, data: bytes) -> bool:
    """Non-blocking version of _write_cached_image."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_disk_io_pool, _write_cached_image, serial, data)


async def _read_local_photo_bytes_async(serial: int) -> Optional[bytes]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_disk_io_pool, local_photos.read_local_photo_bytes, serial)


async def _cache_local_photo(serial: int) -> bool:
    """Copy one local photo into the labeler cache if available."""
    global _active_fills, _total_fills_started
    serial_i = int(serial)
    _active_fills += 1
    _total_fills_started += 1
    try:
        data = await _read_local_photo_bytes_async(serial_i)
        if not data:
            return False
        ok = await _write_cached_image_async(serial_i, data)
        if ok:
            _cached_serials.add(serial_i)
        return ok
    finally:
        _active_fills = max(0, int(_active_fills) - 1)


async def cache_local_image_async(serial: int) -> bool:
    """Ensure one local serial is mirrored into the cache."""
    serial_i = int(serial)
    if serial_i <= 0:
        return False
    if has_cached_image(serial_i):
        _cached_serials.add(serial_i)
        return True

    owner = False
    task: Optional[asyncio.Task] = None
    async with _fill_inflight_lock:
        existing = _fill_inflight.get(serial_i)
        if existing and not existing.done():
            task = existing
        else:
            task = asyncio.create_task(_cache_local_photo(serial_i))
            _fill_inflight[serial_i] = task
            owner = True

    try:
        return bool(await task)
    finally:
        if owner:
            async with _fill_inflight_lock:
                current = _fill_inflight.get(serial_i)
                if current is task:
                    _fill_inflight.pop(serial_i, None)


def download_inflight_count() -> int:
    """Current in-flight cache fill tasks."""
    return sum(1 for task in _fill_inflight.values() if task and not task.done())


def estimate_cache_target_from_budget(budget_gb: float) -> int:
    """Estimate target image count from disk budget and observed average size."""
    budget = max(0.25, float(budget_gb or 0.0))
    budget_bytes = int(budget * 1024 * 1024 * 1024)
    avg_bytes = int(_DEFAULT_ESTIMATED_IMAGE_BYTES)
    try:
        if LABELER_CACHE_DIR.is_dir():
            sizes = [int(p.stat().st_size) for p in LABELER_CACHE_DIR.glob("sn*.jpg") if p.is_file()]
            if sizes:
                sample = sizes[-200:] if len(sizes) > 200 else sizes
                avg_bytes = max(150_000, int(sum(sample) / len(sample)))
    except Exception:
        pass
    return max(10, int(budget_bytes // max(1, avg_bytes)))


async def ensure_cache_filled(
    queue: List[dict],
    target_count: Optional[int] = None,
    *,
    scan_limit: Optional[int] = None,
    concurrency: int = 3,
) -> int:
    """Ensure the labeler cache mirrors the first local serials in the queue."""
    global _cached_serials
    target = max(1, int(target_count or CACHE_SIZE))
    scan_n = max(target, int(scan_limit or max(target, CACHE_SIZE)))

    async with _fill_lock:
        LABELER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()
        _cached_serials = await loop.run_in_executor(_disk_io_pool, _scan_cache)

        local_serials = local_photos.local_serials(force_refresh=False)
        desired_serials: set[int] = set()
        to_cache: List[int] = []

        for item in (queue or [])[:scan_n]:
            try:
                sn = int(item.get("serial") or 0)
            except Exception:
                sn = 0
            if sn <= 0 or sn not in local_serials or sn in desired_serials:
                continue
            desired_serials.add(sn)
            if sn not in _cached_serials:
                to_cache.append(sn)
            if len(desired_serials) >= target:
                break

        max_workers = max(1, min(int(concurrency or 1), int(_CACHE_FILL_CONCURRENCY)))
        for i in range(0, len(to_cache), max_workers):
            batch = to_cache[i:i + max_workers]
            results = await asyncio.gather(*(cache_local_image_async(sn) for sn in batch), return_exceptions=True)
            for sn, result in zip(batch, results):
                if result is True:
                    continue
                if isinstance(result, Exception):
                    log_action("labeler_cache_fill_error", f"sn={int(sn)}", f"{type(result).__name__}: {result!r}")

        removed_stale = await loop.run_in_executor(
            _disk_io_pool,
            functools.partial(_evict_if_needed, _CACHE_MAX_FILES, desired_serials, _CACHE_MAX_BYTES),
        )
        if removed_stale > 0:
            _cached_serials = await loop.run_in_executor(_disk_io_pool, _scan_cache)

        return len(_cached_serials)
