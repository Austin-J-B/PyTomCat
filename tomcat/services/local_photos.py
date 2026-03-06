"""Local photo resolver for serial-numbered cat images."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from ..config import settings

_SN_RE = re.compile(r"^sn(\d+)$", re.IGNORECASE)
_INDEX_LOCK = threading.Lock()
_INDEX_PATHS: Dict[int, Path] = {}
_INDEX_SERIALS: Set[int] = set()
_INDEX_NEXT_REFRESH_MONO: float = 0.0
_INDEX_REFRESH_SEC = 10.0
_INDEX_ROOT_SIG: Tuple[str, int, int] = ("", 0, 0)


def photo_root() -> Path:
    """Configured local photo root for labeler reads."""
    raw = str(getattr(settings, "labeler_local_photo_root", "") or "").strip()
    return Path(raw or "./cache/PicsOfCats/Pictures")


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
