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
from concurrent.futures import ThreadPoolExecutor
import functools
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
_CACHE_MAX_BYTES = max(
    0,
    int(float(os.getenv("LABELER_CACHE_MAX_BYTES", "0") or "0")),
)
if _CACHE_MAX_BYTES <= 0:
    _CACHE_MAX_BYTES = int(
        max(1.0, float(os.getenv("LABELER_BOOT_WARM_BUDGET_GB", "12") or "12")) * 1024 * 1024 * 1024
    )
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
_DRIVE_API_ENABLE = str(
    os.getenv("LABELER_DRIVE_API_ENABLE", "0")
).strip().lower() in {"1", "true", "yes", "on"}
if _DRIVE_API_ENABLE and os.name == "nt":
    _allow_windows_drive_api = str(
        os.getenv("LABELER_DRIVE_API_ENABLE_ON_WINDOWS", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not _allow_windows_drive_api:
        _DRIVE_API_ENABLE = False
        log_action("labeler_drive_api_disabled", "platform", "windows_default_off")
_DRIVE_API_TIMEOUT_SEC = max(
    1.0,
    float(os.getenv("LABELER_DRIVE_API_TIMEOUT_SEC", "6") or "6"),
)
_DEFAULT_ESTIMATED_IMAGE_BYTES = max(
    150_000,
    int(os.getenv("LABELER_CACHE_ESTIMATED_IMAGE_BYTES", "1600000") or "1600000"),
)

#Thread pool for disk I/O so file reads/writes never block the event loop.
_DISK_IO_WORKERS = max(2, int(os.getenv("LABELER_CACHE_DISK_IO_WORKERS", "4") or "4"))
_disk_io_pool = ThreadPoolExecutor(
    max_workers=_DISK_IO_WORKERS,
    thread_name_prefix="labeler_disk_io",
)

#Global semaphore limiting total concurrent image downloads across all callers.
_DOWNLOAD_CONCURRENCY = max(
    1, int(os.getenv("LABELER_CACHE_DOWNLOAD_CONCURRENCY", "4") or "4")
)
_global_download_sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
_active_downloads: int = 0
_total_downloads_started: int = 0


def get_download_stats() -> str:
    """Return a diagnostic string of current download activity."""
    return (
        f"active={_active_downloads}/{_DOWNLOAD_CONCURRENCY}; "
        f"inflight_tasks={len(_download_inflight)}; "
        f"total_started={_total_downloads_started}"
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
_last_fetch_path_by_serial: Dict[int, str] = {}
_http_session_lock = asyncio.Lock()
_http_session: Optional[aiohttp.ClientSession] = None
_drive_service_lock = asyncio.Lock()
_drive_service = None
_drive_api_disabled_reason: str = ""


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


async def _get_drive_service():
    """Best-effort lazy init for Drive API client."""
    global _drive_service, _drive_api_disabled_reason
    if not _DRIVE_API_ENABLE:
        return None
    if _drive_api_disabled_reason:
        return None
    if _drive_service is not None:
        return _drive_service
    async with _drive_service_lock:
        if _drive_service is not None:
            return _drive_service
        if _drive_api_disabled_reason:
            return None
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except Exception as e:
            _drive_api_disabled_reason = f"import:{type(e).__name__}"
            return None
        cred_path = str(getattr(settings, "google_service_account_json", "") or "").strip()
        if not cred_path:
            _drive_api_disabled_reason = "missing_google_service_account_json"
            return None
        if not os.path.exists(cred_path):
            _drive_api_disabled_reason = "service_account_file_missing"
            return None
        try:
            creds = service_account.Credentials.from_service_account_file(
                cred_path,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            _drive_service = await asyncio.to_thread(
                lambda: build("drive", "v3", credentials=creds, cache_discovery=False)
            )
            return _drive_service
        except Exception as e:
            _drive_api_disabled_reason = f"build:{type(e).__name__}"
            return None


async def _download_drive_api_media(drive_id: str, timeout_sec: float) -> Optional[bytes]:
    """Attempt Drive API media download using files.get(alt=media)."""
    did = str(drive_id or "").strip()
    if not did:
        return None
    service = await _get_drive_service()
    if service is None:
        return None
    try:
        def _run() -> bytes:
            req = service.files().get_media(fileId=did)
            return req.execute(num_retries=1)
        data = await asyncio.wait_for(asyncio.to_thread(_run), timeout=max(0.5, float(timeout_sec)))
        if isinstance(data, (bytes, bytearray)):
            payload = bytes(data)
            if _looks_like_image(payload):
                return payload
        return None
    except Exception:
        return None


def download_inflight_count() -> int:
    """Current in-flight deduped image downloads."""
    return sum(1 for task in _download_inflight.values() if task and not task.done())


def get_last_fetch_path(serial: int) -> str:
    """Return last fetch path tag for a serial."""
    try:
        return str(_last_fetch_path_by_serial.get(int(serial)) or "")
    except Exception:
        return ""


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


def _evict_if_needed(max_files: int, keep_serials: set[int], max_bytes: int = 0) -> int:
    """Evict oldest cached files until count/bytes are within limits, preferring non-kept serials."""
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

        # Prefer evicting files not in the active desired queue first.
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
    """Return True if cached bytes exist on disk for this serial. Safe to call from threads."""
    try:
        return _cache_path(int(serial)).is_file()
    except Exception:
        return False


async def has_cached_image_async(serial: int) -> bool:
    """Non-blocking version of has_cached_image for async callers."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_disk_io_pool, has_cached_image, serial)


def _write_cached_image(serial: int, data: bytes) -> bool:
    """Write image bytes to cache file. Safe to call from threads.

    Returns True on success.
    """
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


async def _download_image(
    url: str,
    timeout_sec: float = 10.0,
    *,
    log_errors: bool = _CACHE_VERBOSE,
    bypass_backoff: bool = False,
    max_attempts: int = 2,
    source_out: Optional[Dict[str, str]] = None,
) -> Optional[bytes]:
    """Download image from URL (typically Google Drive).

    Acquires the global download semaphore to limit total concurrent downloads.
    """
    async with _global_download_sem:
        global _active_downloads, _total_downloads_started
        _active_downloads += 1
        _total_downloads_started += 1
        try:
            return await _download_image_inner(
                url, timeout_sec,
                log_errors=log_errors,
                bypass_backoff=bypass_backoff,
                max_attempts=max_attempts,
                source_out=source_out,
            )
        finally:
            _active_downloads -= 1


async def _download_image_inner(
    url: str,
    timeout_sec: float = 10.0,
    *,
    log_errors: bool = _CACHE_VERBOSE,
    bypass_backoff: bool = False,
    max_attempts: int = 2,
    source_out: Optional[Dict[str, str]] = None,
) -> Optional[bytes]:
    """Inner download logic after semaphore is acquired."""
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
        drive_api_attempted = False
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
            if drive_id and _DRIVE_API_ENABLE:
                req_timeout_sec = float(_DRIVE_API_TIMEOUT_SEC)
                if drive_deadline_mono > 0.0:
                    remaining = float(drive_deadline_mono) - time.monotonic()
                    if remaining > 0.0:
                        req_timeout_sec = max(0.5, min(req_timeout_sec, float(remaining)))
                drive_api_attempted = True
                api_data = await _download_drive_api_media(drive_id, req_timeout_sec)
                if api_data:
                    _record_download_success()
                    _record_url_download_success(url)
                    if isinstance(source_out, dict):
                        source_out["path"] = "drive_api"
                    return api_data

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
                    if isinstance(source_out, dict):
                        if drive_api_attempted and drive_id:
                            source_out["path"] = "drive_api_fallback"
                        else:
                            source_out["path"] = "proxy"
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


async def ensure_cache_filled(
    queue: list[dict],
    target_count: Optional[int] = None,
    *,
    scan_limit: Optional[int] = None,
    concurrency: int = 3,
) -> int:
    """Background task: ensure cache has up to target_count images from queue.
    
    Args:
        queue: List of {"serial": int, "url": str} dicts
        target_count: How many to keep cached (default: CACHE_SIZE)
    
    Returns:
        Number of images currently cached
    """
    global _cached_serials
    target = int(target_count or CACHE_SIZE)
    target = max(1, target)
    scan_n = int(scan_limit or max(target, CACHE_SIZE))
    scan_n = max(target, scan_n)
    
    #Use lock to prevent multiple concurrent fills
    async with _fill_lock:
        #Create cache dir if needed
        LABELER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        
        t0 = time.perf_counter()
        #Refresh in-memory set from disk (offloaded to prevent blocking event loop)
        loop = asyncio.get_running_loop()
        _cached_serials = await loop.run_in_executor(_disk_io_pool, _scan_cache)
        t_scan = time.perf_counter()
        
        removed_stale = 0

        #Download missing images up to target
        desired_serials: set[int] = set()
        to_download = []
        scan_items = queue[:scan_n]
        for item in scan_items[:target]:
            sn = item.get("serial")
            url = item.get("url")
            if isinstance(sn, int):
                desired_serials.add(sn)
            if sn and url and sn not in _cached_serials:
                to_download.append((sn, url))
        
        t_filter = time.perf_counter()
        if (t_filter - t0) > 0.5:
            log_action("labeler_cache_ensure_slow_prep", "ensure_cache_filled", f"total_ms={int((t_filter-t0)*1000)}; scan_ms={int((t_scan-t0)*1000)}; filter_ms={int((t_filter-t_scan)*1000)}")

        downloaded_ok = 0
        bytes_downloaded = 0
        download_failed = 0
        write_failed = 0
        fail_serials: list[str] = []
        
        #Download concurrently with bounded workers.
        max_workers = max(1, int(concurrency or 1))

        async def download_one(sn: int, url: str):
            nonlocal downloaded_ok, bytes_downloaded, download_failed, write_failed
            data = await _download_image(url, log_errors=_CACHE_VERBOSE)
            if data:
                ok = await _write_cached_image_async(sn, data)
                if ok:
                    _cached_serials.add(sn)
                    downloaded_ok += 1
                    bytes_downloaded += len(data)
                else:
                    write_failed += 1
                    log_action("labeler_cache_write_error", f"sn{sn}", "write_failed")
            else:
                download_failed += 1
                if len(fail_serials) < 8:
                    fail_serials.append(f"sn{sn}")
        
        #Process in batches
        for i in range(0, len(to_download), max_workers):
            batch = to_download[i:i + max_workers]
            await asyncio.gather(*[download_one(sn, url) for sn, url in batch])

        # Only evict when cache grows too large; do not churn on queue changes.
        loop = asyncio.get_running_loop()
        removed_stale += await loop.run_in_executor(
            _disk_io_pool,
            functools.partial(_evict_if_needed, _CACHE_MAX_FILES, desired_serials, _CACHE_MAX_BYTES),
        )

        return len(_cached_serials)


async def get_or_download(
    serial: int,
    url: str,
    *,
    bypass_backoff: bool = False,
    max_attempts: int = 2,
) -> Optional[bytes]:
    """Get image from cache, or download and cache it."""
    #Try cache first (non-blocking)
    serial_i = int(serial)
    data = await get_cached_image_async(serial)
    if data:
        _last_fetch_path_by_serial[serial_i] = "hit"
        return data

    owner = False
    task: Optional[asyncio.Task] = None
    source_out: Dict[str, str] = {}
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
                    source_out=source_out,
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
        ok = await _write_cached_image_async(serial_i, data)
        if ok:
            _cached_serials.add(serial_i)
        src = str(source_out.get("path") or "").strip().lower() if owner else ""
        if src:
            _last_fetch_path_by_serial[serial_i] = src
        elif serial_i not in _last_fetch_path_by_serial:
            _last_fetch_path_by_serial[serial_i] = "proxy"
    return data
