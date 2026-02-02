"""Labeler image cache - pre-downloads images for instant labeling UI.

Caches ~15 images locally in cache/labeler/ so the labeling UI doesn't
have to wait for Google Drive on every image load.
"""
from __future__ import annotations
import asyncio
import aiohttp
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from ..config import settings
from ..logger import log_action

#Cache config
LABELER_CACHE_DIR = Path("cache") / "labeler"
CACHE_SIZE = getattr(settings, "labeler_cache_size", 15)

#In-memory tracking of what's currently cached
_cached_serials: set[int] = set()
_fill_lock = asyncio.Lock()


def _cache_path(serial: int) -> Path:
    """Path to cached image file."""
    return LABELER_CACHE_DIR / f"sn{str(serial).zfill(4)}.jpg"

def _extract_drive_id(url: str) -> Optional[str]:
    """Extract a Google Drive file id from a share URL."""
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
    """Best-effort check for common image formats."""
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
    """Return cached image bytes, or None if not cached."""
    p = _cache_path(serial)
    if p.is_file():
        try:
            return p.read_bytes()
        except Exception:
            return None
    return None


async def _download_image(url: str, timeout_sec: float = 10.0) -> Optional[bytes]:
    """Download image from URL (typically Google Drive)."""
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        headers = {"User-Agent": "TomCatLabeler/1.0"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
            candidates = [url]
            drive_id = _extract_drive_id(url)
            if drive_id:
                candidates.extend([
                    f"https://drive.google.com/uc?export=download&id={drive_id}",
                    f"https://drive.google.com/uc?export=view&id={drive_id}",
                    f"https://drive.usercontent.google.com/download?id={drive_id}&export=download",
                ])

            seen = set()
            for candidate in candidates:
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                async with sess.get(candidate, allow_redirects=True) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()
                    if _looks_like_image(data, resp.headers.get("Content-Type")):
                        return data
        return None
    except Exception as e:
        log_action("labeler_cache_download_error", url[:50], str(e))
        return None


async def ensure_cache_filled(queue: list[dict], target_count: Optional[int] = None) -> int:
    """Background task: ensure cache has up to target_count images from queue.
    
    Args:
        queue: List of {"serial": int, "url": str} dicts
        target_count: How many to keep cached (default: CACHE_SIZE)
    
    Returns:
        Number of images currently cached
    """
    global _cached_serials
    target = target_count or CACHE_SIZE
    
    #Use lock to prevent multiple concurrent fills
    async with _fill_lock:
        #Create cache dir if needed
        LABELER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        
        #Refresh in-memory set from disk
        _cached_serials = _scan_cache()
        
        #Remove cached images not in current queue (stale)
        queue_serials = {item["serial"] for item in queue if isinstance(item.get("serial"), int)}
        for sn in list(_cached_serials):
            if sn not in queue_serials:
                try:
                    _cache_path(sn).unlink(missing_ok=True)
                    _cached_serials.discard(sn)
                except Exception:
                    pass
        
        #Download missing images up to target
        to_download = []
        for item in queue[:target]:
            sn = item.get("serial")
            url = item.get("url")
            if sn and url and sn not in _cached_serials:
                to_download.append((sn, url))
        
        #Download concurrently (but limit to 5 at a time)
        async def download_one(sn: int, url: str):
            data = await _download_image(url)
            if data:
                try:
                    _cache_path(sn).write_bytes(data)
                    _cached_serials.add(sn)
                    log_action("labeler_cache_ok", f"sn{sn}", f"len={len(data)}")
                except Exception as e:
                    log_action("labeler_cache_write_error", f"sn{sn}", str(e))
        
        #Process in batches of 5
        for i in range(0, len(to_download), 5):
            batch = to_download[i:i+5]
            await asyncio.gather(*[download_one(sn, url) for sn, url in batch])
        
        return len(_cached_serials)


async def get_or_download(serial: int, url: str) -> Optional[bytes]:
    """Get image from cache, or download and cache it."""
    #Try cache first
    data = get_cached_image(serial)
    if data:
        return data
    
    #Download and cache
    data = await _download_image(url)
    if data:
        try:
            LABELER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _cache_path(serial).write_bytes(data)
            _cached_serials.add(serial)
        except Exception:
            pass
    return data
