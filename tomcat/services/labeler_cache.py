"""Labeler image cache - pre-downloads images for instant labeling UI.

Caches ~15 images locally in cache/labeler/ so the labeling UI doesn't
have to wait for Google Drive on every image load.
"""
from __future__ import annotations
import asyncio
import aiohttp
import os
import time
import re
import html as html_lib
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, parse_qs

from ..config import settings
from ..logger import log_action

#Cache config
LABELER_CACHE_DIR = Path("cache") / "labeler"
CACHE_SIZE = getattr(settings, "labeler_cache_size", 15)
_CACHE_VERBOSE = str(os.getenv("LABELER_CACHE_VERBOSE_LOGS", "0")).strip().lower() in {"1", "true", "yes", "on"}
_CACHE_MAX_FILES = max(CACHE_SIZE, int(os.getenv("LABELER_CACHE_MAX_FILES", "300") or "300"))
_DOWNLOAD_BACKOFF_BASE_SEC = max(
    1.0, float(os.getenv("LABELER_CACHE_DOWNLOAD_BACKOFF_BASE_SEC", "5") or "5")
)
_DOWNLOAD_BACKOFF_MAX_SEC = max(
    _DOWNLOAD_BACKOFF_BASE_SEC,
    float(os.getenv("LABELER_CACHE_DOWNLOAD_BACKOFF_MAX_SEC", "60") or "60"),
)
_URL_BACKOFF_BASE_SEC = max(
    1.0, float(os.getenv("LABELER_CACHE_URL_BACKOFF_BASE_SEC", "12") or "12")
)
_URL_BACKOFF_MAX_SEC = max(
    _URL_BACKOFF_BASE_SEC,
    float(os.getenv("LABELER_CACHE_URL_BACKOFF_MAX_SEC", "300") or "300"),
)
_ERROR_LOG_COOLDOWN_SEC = max(
    1.0, float(os.getenv("LABELER_CACHE_ERROR_LOG_COOLDOWN_SEC", "45") or "45")
)
_ERROR_LOG_WINDOW_SEC = max(
    _ERROR_LOG_COOLDOWN_SEC,
    float(os.getenv("LABELER_CACHE_ERROR_LOG_WINDOW_SEC", "60") or "60"),
)
_ERROR_LOG_MAX_PER_WINDOW = max(
    1, int(os.getenv("LABELER_CACHE_ERROR_LOG_MAX_PER_WINDOW", "6") or "6")
)
_HTTP_CONN_LIMIT = max(
    8, int(os.getenv("LABELER_CACHE_HTTP_CONN_LIMIT", "32") or "32")
)
_HTTP_CONN_LIMIT_PER_HOST = max(
    2, int(os.getenv("LABELER_CACHE_HTTP_CONN_LIMIT_PER_HOST", "12") or "12")
)
_HTTP_DNS_CACHE_TTL_SEC = max(
    0, int(os.getenv("LABELER_CACHE_HTTP_DNS_CACHE_TTL_SEC", "300") or "300")
)
_DRIVE_FETCH_TOTAL_TIMEOUT_SEC = max(
    2.0,
    float(os.getenv("LABELER_CACHE_DRIVE_FETCH_TOTAL_TIMEOUT_SEC", "8") or "8"),
)
_DRIVE_FETCH_TOTAL_TIMEOUT_BYPASS_SEC = max(
    _DRIVE_FETCH_TOTAL_TIMEOUT_SEC,
    float(os.getenv("LABELER_CACHE_DRIVE_FETCH_TOTAL_TIMEOUT_BYPASS_SEC", "16") or "16"),
)
_DRIVE_FETCH_PER_REQUEST_TIMEOUT_SEC = max(
    0.8,
    float(os.getenv("LABELER_CACHE_DRIVE_FETCH_PER_REQUEST_TIMEOUT_SEC", "3") or "3"),
)
_DRIVE_FETCH_PER_REQUEST_TIMEOUT_BYPASS_SEC = max(
    _DRIVE_FETCH_PER_REQUEST_TIMEOUT_SEC,
    float(os.getenv("LABELER_CACHE_DRIVE_FETCH_PER_REQUEST_TIMEOUT_BYPASS_SEC", "6") or "6"),
)

#In-memory tracking of what's currently cached
_cached_serials: set[int] = set()
_fill_lock = asyncio.Lock()
_download_fail_streak: int = 0
_download_backoff_until_mono: float = 0.0
_url_fail_streak: dict[str, int] = {}
_url_backoff_until_mono: dict[str, float] = {}
_error_log_next_mono: dict[str, float] = {}
_error_window_start_mono: float = 0.0
_error_window_logged: int = 0
_error_window_suppressed: int = 0
_download_inflight_lock = asyncio.Lock()
_download_inflight: Dict[int, asyncio.Task] = {}
_http_session_lock = asyncio.Lock()
_http_session: Optional[aiohttp.ClientSession] = None


def _record_download_success() -> None:
    global _download_fail_streak, _download_backoff_until_mono
    _download_fail_streak = 0
    _download_backoff_until_mono = 0.0


def _record_download_failure() -> None:
    global _download_fail_streak, _download_backoff_until_mono
    _download_fail_streak += 1
    if _download_fail_streak < 3:
        return
    step = min(8, _download_fail_streak - 3)
    delay = min(_DOWNLOAD_BACKOFF_MAX_SEC, _DOWNLOAD_BACKOFF_BASE_SEC * (2 ** step))
    _download_backoff_until_mono = max(_download_backoff_until_mono, time.monotonic() + float(delay))


def _download_backoff_key(url: str) -> str:
    drive_id = _extract_drive_id(url)
    if drive_id:
        return f"drive:{drive_id}"
    u = str(url or "").strip()
    if len(u) > 220:
        return u[:220]
    return u


def _record_url_download_success(url: str) -> None:
    key = _download_backoff_key(url)
    if not key:
        return
    _url_fail_streak.pop(key, None)
    _url_backoff_until_mono.pop(key, None)


def _record_url_download_failure(url: str) -> None:
    key = _download_backoff_key(url)
    if not key:
        return
    streak = int(_url_fail_streak.get(key, 0)) + 1
    _url_fail_streak[key] = streak
    step = min(8, max(0, streak - 1))
    delay = min(_URL_BACKOFF_MAX_SEC, _URL_BACKOFF_BASE_SEC * (2 ** step))
    _url_backoff_until_mono[key] = max(
        float(_url_backoff_until_mono.get(key, 0.0)),
        time.monotonic() + float(delay),
    )


def _is_url_backoff_active(url: str) -> bool:
    key = _download_backoff_key(url)
    if not key:
        return False
    until = float(_url_backoff_until_mono.get(key, 0.0))
    now = time.monotonic()
    if until <= now:
        _url_backoff_until_mono.pop(key, None)
        return False
    return True


def _log_download_error_throttled(url: str, error: Exception) -> None:
    global _error_window_start_mono, _error_window_logged, _error_window_suppressed
    now = time.monotonic()
    if _error_window_start_mono <= 0.0:
        _error_window_start_mono = now
    elapsed = now - float(_error_window_start_mono)
    if elapsed >= float(_ERROR_LOG_WINDOW_SEC):
        if _error_window_suppressed > 0:
            log_action(
                "labeler_cache_download_error_suppressed",
                "window",
                f"suppressed={_error_window_suppressed}; window_s={int(round(elapsed))}",
            )
        _error_window_start_mono = now
        _error_window_logged = 0
        _error_window_suppressed = 0

    key = _download_backoff_key(url) or "unknown"
    next_allowed = float(_error_log_next_mono.get(key, 0.0))
    if now < next_allowed or _error_window_logged >= int(_ERROR_LOG_MAX_PER_WINDOW):
        _error_window_suppressed += 1
        return
    _error_log_next_mono[key] = now + float(_ERROR_LOG_COOLDOWN_SEC)
    _error_window_logged += 1
    log_action("labeler_cache_download_error", url[:50], f"{type(error).__name__}: {error!r}")


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


def _is_drive_like_url(url: str) -> bool:
    """Return True for Google Drive/googleusercontent URLs used by the labeler."""
    try:
        parsed = urlparse(str(url or ""))
        netloc = str(parsed.netloc or "").lower()
    except Exception:
        return False
    return (
        "drive.google.com" in netloc
        or "drive.usercontent.google.com" in netloc
        or "googleusercontent.com" in netloc
    )


def _extract_drive_html_image_url(page_html: str) -> Optional[str]:
    """Best-effort parse for direct image URL from Google Drive HTML pages."""
    text = str(page_html or "")
    if not text:
        return None
    patterns = [
        r'<meta[^>]+property=["\\\']og:image["\\\'][^>]+content=["\\\']([^"\\\']+)["\\\']',
        r'"(https?:\\\\/\\\\/[^"\\\']*googleusercontent[^"\\\']+)"',
        r"'(https?:\\\\/\\\\/[^\"']*googleusercontent[^\"']+)'",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        url = m.group(1)
        url = html_lib.unescape(str(url or "").strip())
        url = url.replace("\\\\/", "/").replace("\\u003d", "=").replace("\\u0026", "&")
        if url.startswith("http"):
            return url
    return None


def _is_drive_quota_page(page_html: str) -> bool:
    """Detect Drive quota/abuse interstitial pages that are not direct images."""
    text = str(page_html or "").lower()
    if not text:
        return False
    return (
        "quota exceeded" in text
        or "too many users have viewed or downloaded this file" in text
        or "can't view or download this file at this time" in text
    )


def _drive_thumbnail_candidates(drive_id: str) -> List[str]:
    """Fallback endpoints that often remain fetchable when uc/export links are throttled."""
    did = str(drive_id or "").strip()
    if not did:
        return []
    return [
        f"https://drive.google.com/thumbnail?id={did}&sz=w2560",
        f"https://lh3.googleusercontent.com/d/{did}=w2560",
    ]


def _drive_download_candidates(url: str, drive_id: str) -> List[str]:
    """Return deduped Drive candidate URLs ordered to avoid slow wrapper pages first."""
    original = str(url or "").strip()
    did = str(drive_id or "").strip()
    if not did:
        return [original] if original else []

    direct_candidates = [
        *_drive_thumbnail_candidates(did),
        f"https://drive.usercontent.google.com/download?id={did}&export=download",
        f"https://drive.usercontent.google.com/download?id={did}&export=view",
        f"https://drive.google.com/uc?export=download&id={did}",
        f"https://drive.google.com/uc?export=view&id={did}",
    ]
    # Many /file/.../view links return HTML wrappers or hang longer than direct/thumbnail URLs.
    # Prefer direct endpoints first unless the original URL is already a direct image URL.
    if _is_drive_like_url(original) and "drive.google.com" in str(urlparse(original).netloc or "").lower():
        ordered = [*direct_candidates, original]
    else:
        ordered = [original, *direct_candidates]

    seen: set[str] = set()
    uniq: List[str] = []
    for candidate in ordered:
        c = str(candidate or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        uniq.append(c)
    return uniq


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


async def _get_http_session() -> aiohttp.ClientSession:
    """Shared aiohttp session for labeler image fetches (connection reuse matters for Drive)."""
    global _http_session
    sess = _http_session
    if sess is not None and not sess.closed:
        return sess
    async with _http_session_lock:
        sess = _http_session
        if sess is not None and not sess.closed:
            return sess
        connector = aiohttp.TCPConnector(
            limit=int(_HTTP_CONN_LIMIT),
            limit_per_host=int(_HTTP_CONN_LIMIT_PER_HOST),
            ttl_dns_cache=int(_HTTP_DNS_CACHE_TTL_SEC),
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=None)
        _http_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": "TomCatLabeler/1.0"},
        )
        return _http_session


async def close_http_session() -> None:
    """Close the shared HTTP session used by labeler cache downloads."""
    global _http_session
    async with _http_session_lock:
        sess = _http_session
        _http_session = None
    if sess is not None and not sess.closed:
        await sess.close()


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


def _evict_if_needed(max_files: int, keep_serials: set[int]) -> int:
    """Evict oldest cached files until count <= max_files, preferring non-kept serials."""
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
            entries.append((sn, mt, p))
        if len(entries) <= int(max_files):
            return 0

        # Prefer evicting files not in the active desired queue first.
        non_keep = sorted([e for e in entries if e[0] not in keep_serials], key=lambda t: t[1])
        keep = sorted([e for e in entries if e[0] in keep_serials], key=lambda t: t[1])
        ordered = non_keep + keep
        need = max(0, len(entries) - int(max_files))
        removed = 0
        for sn, _, p in ordered[:need]:
            try:
                p.unlink(missing_ok=True)
                _cached_serials.discard(sn)
                removed += 1
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
    """Return cached image bytes, or None if not cached."""
    p = _cache_path(serial)
    if p.is_file():
        try:
            return p.read_bytes()
        except Exception:
            return None
    return None


def has_cached_image(serial: int) -> bool:
    """Return True if cached bytes exist on disk for this serial."""
    try:
        return _cache_path(int(serial)).is_file()
    except Exception:
        return False


async def _download_image(
    url: str,
    timeout_sec: float = 10.0,
    *,
    log_errors: bool = _CACHE_VERBOSE,
    bypass_backoff: bool = False,
    max_attempts: int = 2,
) -> Optional[bytes]:
    """Download image from URL (typically Google Drive)."""
    if not bypass_backoff and _is_url_backoff_active(url):
        return None
    if not bypass_backoff and time.monotonic() < float(_download_backoff_until_mono):
        return None
    try:
        sess = await _get_http_session()
        drive_id = _extract_drive_id(url)
        if drive_id:
            uniq_candidates = _drive_download_candidates(url, drive_id)
        else:
            uniq_candidates = [str(url or "").strip()]

        attempts = max(1, int(max_attempts))
        last_error: Optional[Exception] = None
        drive_deadline_mono: float = 0.0
        if drive_id:
            total_budget = (
                float(_DRIVE_FETCH_TOTAL_TIMEOUT_BYPASS_SEC)
                if bypass_backoff
                else float(_DRIVE_FETCH_TOTAL_TIMEOUT_SEC)
            )
            drive_deadline_mono = time.monotonic() + max(1.0, float(total_budget))
            per_request_timeout = (
                float(_DRIVE_FETCH_PER_REQUEST_TIMEOUT_BYPASS_SEC)
                if bypass_backoff
                else float(_DRIVE_FETCH_PER_REQUEST_TIMEOUT_SEC)
            )
        else:
            per_request_timeout = max(0.5, float(timeout_sec))

        async def _fetch_image_only(candidate_url: str, req_timeout_sec: float) -> Optional[bytes]:
            nonlocal last_error
            timeout = aiohttp.ClientTimeout(total=max(0.5, float(req_timeout_sec)))
            try:
                async with sess.get(candidate_url, allow_redirects=True, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
                    if _looks_like_image(data, resp.headers.get("Content-Type")):
                        return data
                    return None
            except Exception as e:
                last_error = e
                return None

        async def _fetch_candidate(candidate_url: str, req_timeout_sec: float) -> Tuple[Optional[bytes], Optional[str]]:
            nonlocal last_error
            timeout = aiohttp.ClientTimeout(total=max(0.5, float(req_timeout_sec)))
            try:
                async with sess.get(candidate_url, allow_redirects=True, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None, None
                    data = await resp.read()
                    if _looks_like_image(data, resp.headers.get("Content-Type")):
                        return data, candidate_url
                    # Some Drive "view" links return HTML wrappers.
                    ctype = str(resp.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype:
                        try:
                            page = data.decode("utf-8", errors="ignore")
                        except Exception:
                            page = ""
                        img_url = _extract_drive_html_image_url(page)
                        nested_timeout = max(0.5, min(float(req_timeout_sec), float(per_request_timeout)))
                        if img_url:
                            img_data = await _fetch_image_only(img_url, nested_timeout)
                            if img_data:
                                return img_data, img_url
                        if drive_id and _is_drive_quota_page(page):
                            for thumb_url in _drive_thumbnail_candidates(drive_id):
                                thumb_data = await _fetch_image_only(thumb_url, nested_timeout)
                                if thumb_data:
                                    return thumb_data, thumb_url
            except Exception as e:
                last_error = e
            return None, None

        budget_exhausted = False
        for attempt in range(1, attempts + 1):
            for candidate in uniq_candidates:
                req_timeout_sec = float(per_request_timeout)
                if drive_deadline_mono > 0.0:
                    remaining = float(drive_deadline_mono) - time.monotonic()
                    if remaining <= 0.0:
                        budget_exhausted = True
                        break
                    req_timeout_sec = max(0.5, min(float(per_request_timeout), float(remaining)))
                data, success_key = await _fetch_candidate(candidate, req_timeout_sec)
                if data:
                    _record_download_success()
                    if success_key:
                        _record_url_download_success(success_key)
                        if success_key != candidate:
                            _record_url_download_success(candidate)
                    else:
                        _record_url_download_success(candidate)
                    return data
            if budget_exhausted:
                break
            if attempt < attempts:
                sleep_for = 0.2 * attempt
                if drive_deadline_mono > 0.0:
                    remaining = float(drive_deadline_mono) - time.monotonic()
                    if remaining <= 0.0:
                        budget_exhausted = True
                        break
                    sleep_for = min(float(sleep_for), max(0.0, float(remaining)))
                if sleep_for > 0:
                    await asyncio.sleep(float(sleep_for))
        if budget_exhausted and last_error is None and drive_id:
            last_error = asyncio.TimeoutError("Drive fetch budget exhausted")
        _record_download_failure()
        _record_url_download_failure(url)
        if log_errors and last_error is not None:
            _log_download_error_throttled(url, last_error)
        return None
    except Exception as e:
        _record_download_failure()
        _record_url_download_failure(url)
        if log_errors:
            _log_download_error_throttled(url, e)
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
        
        removed_stale = 0

        #Download missing images up to target
        desired_serials: set[int] = set()
        to_download = []
        for item in queue[:target]:
            sn = item.get("serial")
            url = item.get("url")
            if isinstance(sn, int):
                desired_serials.add(sn)
            if sn and url and sn not in _cached_serials:
                to_download.append((sn, url))

        downloaded_ok = 0
        bytes_downloaded = 0
        download_failed = 0
        write_failed = 0
        fail_serials: list[str] = []
        
        #Download concurrently (but limit to 5 at a time)
        async def download_one(sn: int, url: str):
            nonlocal downloaded_ok, bytes_downloaded, download_failed, write_failed
            data = await _download_image(url, log_errors=_CACHE_VERBOSE)
            if data:
                try:
                    _cache_path(sn).write_bytes(data)
                    _cached_serials.add(sn)
                    downloaded_ok += 1
                    bytes_downloaded += len(data)
                except Exception as e:
                    write_failed += 1
                    log_action("labeler_cache_write_error", f"sn{sn}", str(e))
            else:
                download_failed += 1
                if len(fail_serials) < 8:
                    fail_serials.append(f"sn{sn}")
        
        #Process in batches of 5
        for i in range(0, len(to_download), 5):
            batch = to_download[i:i+5]
            await asyncio.gather(*[download_one(sn, url) for sn, url in batch])

        # Only evict when cache grows too large; do not churn on queue changes.
        removed_stale += _evict_if_needed(_CACHE_MAX_FILES, desired_serials)

        return len(_cached_serials)


async def get_or_download(
    serial: int,
    url: str,
    *,
    bypass_backoff: bool = False,
    max_attempts: int = 2,
) -> Optional[bytes]:
    """Get image from cache, or download and cache it."""
    #Try cache first
    data = get_cached_image(serial)
    if data:
        return data

    owner = False
    task: Optional[asyncio.Task] = None
    serial_i = int(serial)
    async with _download_inflight_lock:
        existing = _download_inflight.get(serial_i)
        if existing and not existing.done():
            task = existing
        else:
            task = asyncio.create_task(
                _download_image(
                    url,
                    bypass_backoff=bypass_backoff,
                    max_attempts=max_attempts,
                )
            )
            _download_inflight[serial_i] = task
            owner = True

    try:
        data = await task
    finally:
        if owner:
            async with _download_inflight_lock:
                current = _download_inflight.get(serial_i)
                if current is task:
                    _download_inflight.pop(serial_i, None)

    if data:
        try:
            LABELER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _cache_path(serial).write_bytes(data)
            _cached_serials.add(serial)
        except Exception:
            pass
    return data
