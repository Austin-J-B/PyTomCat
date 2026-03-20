"""Compatibility shim for labeler image access.

The labeler used to mirror local source images into ``cache/labeler`` as
``sn####.jpg`` files. The local photo library is now the source of truth, so
this module keeps the same API shape while reading directly from
``local_photos``. The ``cache/labeler`` directory remains in use for JSON state
files such as flag queues and blacklists.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from . import local_photos

LABELER_CACHE_DIR = Path("cache") / "labeler"

_DISK_IO_WORKERS = 4
_disk_io_pool = ThreadPoolExecutor(
    max_workers=_DISK_IO_WORKERS,
    thread_name_prefix="labeler_disk_io",
)

_cached_serials: set[int] = set()


def _refresh_cached_serials(*, force_refresh: bool = False) -> set[int]:
    """Track locally available serials for callers that still inspect this set."""
    global _cached_serials
    try:
        _cached_serials = local_photos.local_serials(force_refresh=force_refresh)
    except Exception:
        _cached_serials = set()
    return set(_cached_serials)


def prune_legacy_image_cache() -> int:
    """Delete legacy mirrored JPGs while leaving labeler JSON state intact."""
    removed = 0
    if not LABELER_CACHE_DIR.is_dir():
        return 0
    for path in LABELER_CACHE_DIR.glob("sn*.jpg"):
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass
    _refresh_cached_serials(force_refresh=False)
    return removed


def shutdown_cache_executor(*, wait: bool = False) -> None:
    """Release the persistent executor during process shutdown."""
    global _disk_io_pool
    pool = _disk_io_pool
    if pool is None:
        return
    _disk_io_pool = None
    try:
        pool.shutdown(wait=wait, cancel_futures=True)
    except Exception:
        pass


def get_download_stats() -> str:
    """Return inert cache diagnostics for existing logging hooks."""
    return "active=0/0; inflight_tasks=0; total_started=0"


def clear_cache() -> int:
    """Clear only legacy mirrored image files, preserving labeler JSON state."""
    return prune_legacy_image_cache()


def get_cached_image(serial: int) -> Optional[bytes]:
    """Return local source image bytes for this serial."""
    try:
        serial_i = int(serial)
    except Exception:
        return None
    if serial_i <= 0:
        return None
    data = local_photos.read_local_photo_bytes(serial_i)
    if data:
        _cached_serials.add(serial_i)
    else:
        _cached_serials.discard(serial_i)
    return data


async def get_cached_image_async(serial: int) -> Optional[bytes]:
    """Non-blocking version of get_cached_image for async callers."""
    if _disk_io_pool is None:
        return get_cached_image(serial)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_disk_io_pool, get_cached_image, serial)


def has_cached_image(serial: int) -> bool:
    """Return True when a local source image exists for this serial."""
    try:
        serial_i = int(serial)
    except Exception:
        return False
    if serial_i <= 0:
        return False
    exists = local_photos.has_local_photo(serial_i, force_refresh=False)
    if exists:
        _cached_serials.add(serial_i)
    else:
        _cached_serials.discard(serial_i)
    return exists


async def has_cached_image_async(serial: int) -> bool:
    """Non-blocking version of has_cached_image for async callers."""
    if _disk_io_pool is None:
        return has_cached_image(serial)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_disk_io_pool, has_cached_image, serial)


async def cache_local_image_async(serial: int) -> bool:
    """Compatibility shim: local images are already the source of truth."""
    return await has_cached_image_async(serial)


def download_inflight_count() -> int:
    """Current in-flight cache fill tasks."""
    return 0


def estimate_cache_target_from_budget(budget_gb: float) -> int:
    """Return the number of currently available local images."""
    del budget_gb
    return max(10, len(_refresh_cached_serials(force_refresh=False)))


async def ensure_cache_filled(
    queue: List[dict],
    target_count: Optional[int] = None,
    *,
    scan_limit: Optional[int] = None,
    concurrency: int = 3,
) -> int:
    """Compatibility shim for old warm endpoints.

    No image mirroring is needed anymore; this just drops legacy mirror files and
    refreshes the locally available serial set.
    """
    del queue, target_count, scan_limit, concurrency
    prune_legacy_image_cache()
    return len(_refresh_cached_serials(force_refresh=False))


try:
    _refresh_cached_serials(force_refresh=False)
except Exception:
    _cached_serials = set()
