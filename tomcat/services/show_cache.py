"""Local filesystem cache for show-photo responses."""

from __future__ import annotations
import os, re, io, asyncio
from typing import Optional, List, Tuple, Dict
from pathlib import Path
import time

from ..config import settings
from ..logger import log_action
from ..vision import vision as V
from PIL import Image
from .catsheets import get_cat_profile, get_photo_metadata_rows
from . import local_photos

def _primary_label(full_name: str) -> str:
    """Canonicalize labels by taking the first comma-separated cat token."""
    raw = str(full_name or "").strip()
    if not raw:
        return ""
    first = raw.split(",", 1)[0].strip()
    return first or raw


def _display_label(value: str) -> str:
    """Drop the numeric CatDatabase prefix so local CV labels still match."""
    return re.sub(r"^\s*\d+\s*[.)\-:]?\s*", "", str(value or "").strip(), count=1).strip()


def _query_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _display_label(value).lower())


def _row_label_tokens(row: list[str]) -> list[str]:
    raw = ""
    if len(row) > 9:
        raw = str(row[9] or "").strip()
    if not raw and row:
        raw = str(row[0] or "").strip()
    if not raw:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[|,;/]+", raw):
        token = _display_label(part)
        key = _query_key(token)
        if not token or not key or key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _row_matches_query(row: list[str], full_name: str) -> bool:
    key = _query_key(full_name)
    if not key:
        return False
    for token in _row_label_tokens(row):
        if _query_key(token) == key:
            return True
    return False


def _cache_dir_for(cat_id: int) -> str:
    """Return the filesystem path for a given cat cache directory."""
    base = settings.show_cache_dir
    return os.path.join(base, f"{cat_id:03d}")

def _cat_id_from_full(full_name: str) -> Optional[int]:
    """Extract the numeric prefix from a full cat name."""
    m = re.match(r"\s*(\d+)[\.|\s]", full_name or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:  #non-numeric prefix; shouldn't happen with regex match
        return None

def latest_cached_bytes(full_name: str) -> Optional[bytes]:
    """Return the most recent cached JPG bytes for this cat, if present."""
    cid = _cat_id_from_full(full_name)
    if cid is None:
        #Try to resolve via sidecar name index
        if not _NAME_INDEX:
            _build_name_index()
        key = _norm(full_name)
        if key in _NAME_INDEX:
            cid = _NAME_INDEX[key]
    if cid is None:
        return None
    cdir = _cache_dir_for(cid)
    if not os.path.isdir(cdir):
        return None
    best = None
    best_sn = -1
    for fn in os.listdir(cdir):
        if not fn.lower().endswith('.jpg'):
            continue
        m = re.search(r"sn(\d+)", fn)
        sn = int(m.group(1)) if m else -1
        if sn > best_sn:
            best_sn = sn
            best = os.path.join(cdir, fn)
    if not best:
        return None
    try:
        return Path(best).read_bytes()
    except Exception:  #file read failed (deleted, permissions, etc.)
        return None

def _maybe_crop_single(raw: bytes) -> Optional[bytes]:
    """Run the vision cropper and return the single crop if available."""
    try:
        #Temporarily suppress viz_crop logs during bulk cache fills
        from ..config import settings as _settings
        prev = getattr(_settings, 'cv_log_crop', True)
        try:
            _settings.cv_log_crop = False
            crops = V.crop(raw)
        finally:
            _settings.cv_log_crop = prev
        if len(crops) == 1:
            return crops[0]
        return None
    except Exception:  #vision model unavailable or failed; return uncropped
        return None

_PHOTO_METADATA_ROWS_CACHE: Optional[List[List[str]]] = None
_PHOTO_METADATA_ROWS_CACHE_TS: float = 0.0

def _set_photo_metadata_rows_cache(rows: Optional[List[List[str]]]) -> None:
    """Seed the in-memory view of local photo metadata rows."""
    global _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_CACHE_TS
    _PHOTO_METADATA_ROWS_CACHE = rows
    _PHOTO_METADATA_ROWS_CACHE_TS = time.monotonic()


def reset_photo_metadata_cache() -> None:
    """Clear the cached local photo metadata rows so the next call reloads them."""
    global _PHOTO_METADATA_ROWS_CACHE, _PHOTO_METADATA_ROWS_CACHE_TS
    _PHOTO_METADATA_ROWS_CACHE = None
    _PHOTO_METADATA_ROWS_CACHE_TS = 0.0

#--- INSERT IN tomcat/services/show_cache.py ---

async def list_recent_pairs(full_name: str) -> List[Tuple[str, str, int, int]]:
    """Return URL/SERIAL pairs from local photo metadata for a given cat."""
    try:
        # Reuse the existing row caching mechanism, but fetch from local metadata.
        rows = None
        now = time.monotonic()
        ttl = max(1, int(getattr(settings, 'photo_metadata_cache_ttl_sec', 300) or 300))
        
        if _PHOTO_METADATA_ROWS_CACHE is not None and (now - _PHOTO_METADATA_ROWS_CACHE_TS) < ttl:
            rows = _PHOTO_METADATA_ROWS_CACHE
        
        if rows is None:
            rows = get_photo_metadata_rows(ttl)
            if rows:
                _set_photo_metadata_rows_cache(rows)
        if not rows:
            return []

        matches = []
        for r in rows[1:]:
            if len(r) <= 7:
                continue
            if not _row_matches_query(r, full_name):
                continue
            serial = _serial_from_name(str(r[7] or ""))
            if serial <= 0 or not local_photos.has_local_photo(serial):
                continue
            matches.append(r)

        if not matches:
            return []

        #Sort by serial; report reverse_index from the bottom so higher serials show larger numbers
        def parse_serial(row):
            return _serial_from_name(str(row[7] if len(row) > 7 else ""))
        
        matches.sort(key=parse_serial, reverse=True)
        
        total = len(matches)
        out: List[Tuple[str, str, int, int]] = []
        
        for idx, r in enumerate(matches):
            #(URL, Serial, Reverse Index, Total)
            out.append((
                str(r[6] if len(r) > 6 else ""),
                str(r[7] if len(r) > 7 else "0"),
                total - idx,
                total
            ))
            
        return out

    except Exception as e:
        log_action('show_cache_list_error', full_name, str(e))
        return []

def _existing_serials(cat_dir: str) -> set[str]:
    """List serial numbers already cached for a directory."""
    os.makedirs(cat_dir, exist_ok=True)
    serials: set[str] = set()
    for fn in os.listdir(cat_dir):
        m = re.search(r"sn(\d+)", fn)
        if m:
            serials.add(m.group(1))
    return serials

def _cached_jpgs(cat_dir: str) -> list[str]:
    """Return cached JPG filenames for the given cat directory."""
    if not os.path.isdir(cat_dir):
        return []
    return [p for p in os.listdir(cat_dir) if p.lower().endswith('.jpg')]

def _serial_from_name(name: str) -> int:
    """Parse the numeric serial from a cache filename."""
    m = re.search(r"sn(\d+)", name or "", flags=re.IGNORECASE)
    if not m:
        return -1
    try:
        return int(m.group(1))
    except Exception:
        return -1

def _normalize_cached_names(cat_dir: str) -> None:
    """Rename cached files to the serial-based naming scheme used by the current cache."""
    if not os.path.isdir(cat_dir):
        return
    for fn in list(os.listdir(cat_dir)):
        renamed = False
        src = os.path.join(cat_dir, fn)

        # Rename files like 123_sn456.jpg to sn456.jpg.
        m = re.match(r"(\d+)_sn(\d+)(\.[a-z0-9]+)$", fn, flags=re.IGNORECASE)
        if m:
            serial = m.group(2)
            ext = m.group(3).lower()
            new_fn = f"sn{serial.zfill(4)}{ext}"
            renamed = True
        else:
            #Case 2: sn30.jpg -> sn0030.jpg (pad to 4 digits)
            m2 = re.match(r"sn(\d+)(\.[a-z0-9]+)$", fn, flags=re.IGNORECASE)
            if m2 and len(m2.group(1)) < 4:
                serial = m2.group(1)
                ext = m2.group(2).lower()
                new_fn = f"sn{serial.zfill(4)}{ext}"
                renamed = True

        if not renamed:
            continue

        dst = os.path.join(cat_dir, new_fn)
        try:
            if os.path.exists(dst):
                os.remove(src)
            else:
                os.rename(src, dst)
        except Exception:
            continue
        #Rename sidecar JSON if present
        try:
            side_src = os.path.splitext(src)[0] + ".json"
            side_dst = os.path.splitext(dst)[0] + ".json"
            if os.path.exists(side_src):
                if os.path.exists(side_dst):
                    os.remove(side_src)
                else:
                    os.rename(side_src, side_dst)
        except Exception:
            pass

def _prune_cache(cat_dir: str, keep: int) -> int:
    """Cap the cache to at most `keep` JPGs, preferring highest serials."""
    keep = max(0, int(keep))
    files = _cached_jpgs(cat_dir)
    if len(files) <= keep:
        return len(files)
    paths = [os.path.join(cat_dir, f) for f in files]
    paths.sort(key=lambda p: (_serial_from_name(os.path.basename(p)), os.path.getmtime(p) if os.path.exists(p) else 0), reverse=True)
    to_remove = paths[keep:]
    for p in to_remove:
        try:
            os.remove(p)
        except Exception:
            pass
        try:
            mp = os.path.splitext(p)[0] + ".json"
            if os.path.exists(mp):
                os.remove(mp)
        except Exception:
            pass
    return min(keep, len(paths[:keep]))

async def ensure_cat_cache(full_name: str, min_count: Optional[int] = None, exclude_serials: Optional[set[str]] = None, *, prefer_random: bool = False) -> int:
    """Guarantee at least min_count cached photos for a cat, reading local bytes as needed."""
    """Ensure at least min_count images exist for the cat; returns total count after fill."""
    full_name = _primary_label(full_name)
    min_count = max(0, int(min_count or settings.show_cache_per_cat))
    cid = _cat_id_from_full(full_name)
    display_name = None
    if cid is None:
        prof = await get_cat_profile(full_name)
        if isinstance(prof, dict):
            actual = prof.get('actual_name') or ''
            if actual:
                full_name = _primary_label(actual)
            cid = _cat_id_from_full(actual)
            display_name = re.sub(r"^\s*\d+\.\s*", "", str(actual)).strip()
    if cid is None:
        return 0
    cdir = _cache_dir_for(cid)
    os.makedirs(cdir, exist_ok=True)
    _normalize_cached_names(cdir)
    existing = _cached_jpgs(cdir)
    if len(existing) > min_count:
        _prune_cache(cdir, min_count)
        existing = _cached_jpgs(cdir)
    if len(existing) >= min_count:
        return len(existing)
    pairs = await list_recent_pairs(full_name)
    # Shuffle the pool so fresh cache entries do not always dominate.
    try:
        import random as _rand
        _rand.shuffle(pairs)  #vary cache entries across full history
    except Exception:  #shuffle failed; proceed with original order
        pass
    #Try to grab profile once to embed into sidecar metadata (avoid live sheet on send)
    profile_snapshot: Optional[dict] = None
    try:
        prof = await get_cat_profile(full_name)
        if isinstance(prof, dict):
            profile_snapshot = {
                "location": prof.get("location"),
                "behavior": prof.get("behavior"),
                "age": prof.get("age"),
                "sex": prof.get("sex"),
                "tnrd": prof.get("tnrd"),
                "tnr_date": prof.get("tnr_date"),
                "last_seen_date": prof.get("last_seen_date"),
                "last_seen_time": prof.get("last_seen_time"),
                "last_seen_by": prof.get("last_seen_by"),
                "nicknames": prof.get("nicknames"),
                "comments": prof.get("comments"),
            }
    except Exception:  #profile lookup optional; proceed with None
        profile_snapshot = None
    have_serials = _existing_serials(cdir)
    if exclude_serials:
        have_serials = set(have_serials) | {str(s) for s in exclude_serials}

    #Ghost pruning disabled - was causing excessive logging and isn't critical.
    #If stale files accumulate, manual cleanup or a separate maintenance task can handle it.

    total = len(existing)
    for url, serial, reverse_index, total_available in pairs:
        sn = re.sub(r"[^0-9]", "", serial or "") or "0"
        
        if sn in have_serials:
            # Heal stale metadata in existing files without rewriting image bytes.
            try:
                base = f"sn{str(sn).zfill(4)}"
                jp = os.path.join(cdir, f"{base}.json")
                if os.path.exists(jp):
                    import json as _json
                    meta = _json.loads(Path(jp).read_text(encoding='utf-8'))

                    changed = False
                    if meta.get("total_available") != total_available or meta.get("reverse_index") != reverse_index:
                        meta["total_available"] = total_available
                        meta["reverse_index"] = reverse_index
                        changed = True
                    canon_full = str(full_name)
                    canon_disp = display_name or re.sub(r"^\s*\d+\.\s*", "", canon_full).strip()
                    if str(meta.get("full_name") or "").strip() != canon_full:
                        meta["full_name"] = canon_full
                        changed = True
                    if str(meta.get("display_name") or "").strip() != canon_disp:
                        meta["display_name"] = canon_disp
                        changed = True
                    try:
                        existing_cid = int(meta.get("cat_id") or 0)
                    except Exception:
                        existing_cid = 0
                    if existing_cid != int(cid):
                        meta["cat_id"] = int(cid)
                        changed = True
                    if changed:
                        Path(jp).write_text(_json.dumps(meta), encoding='utf-8')
            except Exception:
                pass
            continue

        if total >= min_count:
            continue

        try:
            serial_i = int(sn)
        except Exception:
            continue
        raw = await asyncio.to_thread(local_photos.read_local_photo_bytes, serial_i)
        if not raw:
            log_action('show_cache_local_missing', full_name, f"sn={sn}")
            continue
        #Optional crop during fill; run CV work off the event loop.
        data = raw
        try:
            if bool(getattr(settings, 'show_cache_crop_on_fill', True)):
                cropped = await asyncio.wait_for(
                    asyncio.to_thread(_maybe_crop_single, raw),
                    timeout=12.0,
                )
                data = cropped or raw
        except Exception:
            data = raw
        #Optional: downscale/compress to speed Discord upload
        try:
            mx = int(getattr(settings, 'show_cache_resize_max_dim', 0) or 0)
            if mx > 0:
                im = Image.open(io.BytesIO(data)).convert('RGB')
                w, h = im.size
                if max(w, h) > mx:
                    if w >= h:
                        nw = mx
                        nh = int(h * (mx / float(w)))
                    else:
                        nh = mx
                        nw = int(w * (mx / float(h)))
                    im = im.resize((nw, nh))
                buf = io.BytesIO()
                q = int(getattr(settings, 'show_cache_jpeg_quality', 88) or 88)
                im.save(buf, format='JPEG', quality=q, optimize=True)
            data = buf.getvalue()
        except Exception:
            pass
        base = f"sn{str(sn).zfill(4)}"
        fn = os.path.join(cdir, f"{base}.jpg")
        try:
            with open(fn, 'wb') as f:
                f.write(data)
            # Write sidecar JSON with metadata.
            meta = {
                "serial": serial,
                "reverse_index": reverse_index,
                "total_available": total_available,
                "url": url,
                "cat_id": int(cid),
                "full_name": str(full_name),
                "display_name": display_name or re.sub(r"^\s*\d+\.\s*", "", str(full_name)).strip(),
                "profile": profile_snapshot,
            }
            try:
                with open(os.path.join(cdir, f"{base}.json"), 'w', encoding='utf-8') as jf:
                    import json as _json
                    jf.write(_json.dumps(meta))
            except Exception as e:
                log_action('show_cache_meta_write_error', base, str(e))
            total += 1
            have_serials.add(sn)
        except Exception as e:
            log_action('show_cache_write_error', fn, str(e))
    final = len(_cached_jpgs(cdir))
    if final > min_count:
        final = _prune_cache(cdir, min_count)
    return final

_NAME_INDEX: dict[str, int] = {}

def _norm(s: str) -> str:
    """Normalize strings for case-insensitive matching."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def _build_name_index() -> None:
    """Populate the alias index from cached local sidecar metadata."""
    global _NAME_INDEX
    _NAME_INDEX = {}
    base = settings.show_cache_dir
    if not os.path.isdir(base):
        return
    for sub in os.listdir(base):
        p = os.path.join(base, sub)
        if not os.path.isdir(p):
            continue
        try:
            for jf in os.listdir(p):
                if not jf.lower().endswith('.json'):
                    continue
                try:
                    import json as _json
                    meta = _json.loads(Path(os.path.join(p, jf)).read_text(encoding='utf-8'))
                    full = str(meta.get("full_name") or "").strip()
                    #Skip stale mixed-label sidecars; they can poison fuzzy lookup.
                    if "," in full:
                        continue
                    cid = int(meta.get("cat_id") or int(sub))
                    parsed = _cat_id_from_full(full) if full else None
                    if parsed is not None and parsed != cid:
                        continue
                    disp = str(meta.get("display_name") or "").strip()
                    if not disp and full:
                        disp = re.sub(r"^\s*\d+\.\s*", "", full).strip()
                    if not disp or "," in disp:
                        continue
                    _NAME_INDEX[_norm(disp)] = cid
                    break
                except Exception:
                    continue
        except Exception:
            continue


def rebuild_name_index() -> None:
    """Expose a public hook to refresh the cached name index."""
    _build_name_index()

def _fix_cached_reverse_index(meta: Optional[dict]) -> Optional[dict]:
    """Normalize older sidecar metadata so cached entries use current fields."""
    if isinstance(meta, dict):
        # Older sidecars may store multi-cat labels; normalize to one canonical full name.
        full = str(meta.get("full_name") or "").strip()
        if full:
            canon = _primary_label(full)
            meta["full_name"] = canon
            if canon != full:
                # Legacy mixed-label sidecars carry misleading page counts (often "out of 2").
                # Let refill write fresh indices for the canonical cat.
                meta["reverse_index"] = "?"
                meta["total_available"] = "?"
            if meta.get("display_name"):
                try:
                    meta["display_name"] = re.sub(r"^\s*\d+\.\s*", "", canon).strip()
                except Exception:
                    pass
    #Legacy code inverted the index; the current storage format is already correct:
    #oldest (lowest serial) = 1, newest (highest serial) = total_available
    return meta

def _resolve_cat_id(query: str) -> Optional[int]:
    """Resolve a fuzzy cat query into an ID using the alias index."""
    m = re.match(r"\s*(\d+)[\.|\\s]", query or "")
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    key = _norm(query)
    if not _NAME_INDEX:
        _build_name_index()
    if key in _NAME_INDEX:
        return _NAME_INDEX[key]
    for k, vid in _NAME_INDEX.items():
        if key and key in k:
            return vid
    return None

def _remove_cache_entry(path: str) -> None:
    """Delete one cache JPG and sidecar JSON, best-effort."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    try:
        mp = os.path.splitext(path)[0] + ".json"
        if os.path.exists(mp):
            os.remove(mp)
    except Exception:
        pass


def _meta_is_ambiguous(meta: Optional[dict], expected_cid: Optional[int]) -> bool:
    """Return True when sidecar metadata is too stale/ambiguous to trust."""
    if not isinstance(meta, dict):
        return False
    full = str(meta.get("full_name") or "").strip()
    #Mixed labels can represent another cat and produce wrong embeds.
    if "," in full:
        return True
    if str(meta.get("reverse_index", "")).strip() in {"", "?"}:
        return True
    if str(meta.get("total_available", "")).strip() in {"", "?"}:
        return True
    if expected_cid is None:
        return False
    try:
        expected = int(expected_cid)
    except Exception:
        return False
    cat_id = meta.get("cat_id")
    if cat_id not in (None, ""):
        try:
            if int(cat_id) != expected:
                return True
        except Exception:
            return True
    parsed = _cat_id_from_full(full) if full else None
    if parsed is not None and parsed != expected:
        return True
    return False


def _pop_from_files(files: list[str], expected_cid: Optional[int] = None) -> tuple[Optional[bytes], Optional[dict]]:
    """Pop a random cached file (fast path, no async). Returns (bytes, meta)."""
    from pathlib import Path
    import random as _rand

    candidates = list(files)
    try:
        _rand.shuffle(candidates)
    except Exception:
        candidates = sorted(candidates)

    for path in candidates:
        try:
            data = Path(path).read_bytes()
        except Exception:
            _remove_cache_entry(path)
            continue

        meta: Optional[dict] = None
        try:
            mp = os.path.splitext(path)[0] + ".json"
            if os.path.exists(mp):
                import json as _json
                raw_meta = _json.loads(Path(mp).read_text(encoding='utf-8'))
                if _meta_is_ambiguous(raw_meta, expected_cid):
                    _remove_cache_entry(path)
                    continue
                meta = _fix_cached_reverse_index(raw_meta)
                if _meta_is_ambiguous(meta, expected_cid):
                    _remove_cache_entry(path)
                    continue
        except Exception:
            meta = None

        _remove_cache_entry(path)
        return data, meta

    return None, None

async def pop_one_cached(full_name: str, allow_profile_lookup: bool = True) -> tuple[Optional[bytes], Optional[dict]]:
    """Return and remove one cached image entry, optionally consulting CatDatabase.
    
    Optimized for instant cache hits: if cat ID resolves locally and files exist,
    returns immediately without any CatDatabase reads.
    """
    cid = _cat_id_from_full(full_name)
    if cid is None:
        #Try local profile cache first to avoid scanning cache dirs or hitting Sheets
        try:
            from . import profile_cache as PC
            prof = PC.get_profile_local(full_name)
            if isinstance(prof, dict):
                cid = _cat_id_from_full(prof.get('actual_name') or '')
        except Exception:
            pass
    if cid is None:
        cid = _resolve_cat_id(full_name)
    
    # When the cat ID is already known, check on-disk cache files before Sheets.
    if cid is not None:
        cdir = _cache_dir_for(cid)
        if os.path.isdir(cdir):
            files = [os.path.join(cdir, p) for p in os.listdir(cdir) if p.lower().endswith('.jpg')]
            if files:
                #Fast path: files exist, skip sheet lookup entirely
                return _pop_from_files(files, expected_cid=cid)
    
    #Slow path: need sheet to resolve cat ID
    if cid is None and allow_profile_lookup:
        prof = await get_cat_profile(full_name)
        if isinstance(prof, dict):
            cid = _cat_id_from_full(prof.get('actual_name') or '')
    if cid is None:
        return None, None
    cdir = _cache_dir_for(cid)
    if not os.path.isdir(cdir):
        return None, None
    files = [os.path.join(cdir, p) for p in os.listdir(cdir) if p.lower().endswith('.jpg')]
    if not files:
        return None, None
    return _pop_from_files(files, expected_cid=cid)

async def warm_cache_on_boot() -> None:
    """Background task that primes caches for frequently requested cats."""
    if not settings.show_cache_prefill_on_boot:
        return
    try:
        rows = get_photo_metadata_rows(getattr(settings, 'photo_metadata_cache_ttl_sec', 300))
        if not rows:
            log_action('show_cache_warm_error', 'local_metadata', 'no rows fetched')
            return
    except Exception as e:
        log_action('show_cache_warm_error', 'local_metadata', str(e))
        return
    # Seed row cache so list_recent_pairs doesn't re-scan immediately.
    try:
        _set_photo_metadata_rows_cache(rows)
    except Exception:
        pass
    names: list[str] = []
    try:
        from . import profile_cache as PC
        if PC.cached_count() == 0:
            await PC.refresh_async()
        names = PC.all_actual_names()
    except Exception:
        names = []
    if not names:
        seen: set[str] = set()
        for r in rows[1:]:
            for token in _row_label_tokens(r):
                if token and token not in seen:
                    seen.add(token)
                    names.append(token)
    if settings.show_cache_warm_limit > 0:
        names = names[:settings.show_cache_warm_limit]

    concurrency = max(1, settings.show_cache_warm_concurrency)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for nm in names:
        queue.put_nowait(nm)

    async def _worker() -> None:
        while True:
            try:
                nm = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                cdir = _cache_dir_for(_cat_id_from_full(nm) or 0)
                cnt = len([p for p in os.listdir(cdir) if p.lower().endswith('.jpg')]) if os.path.isdir(cdir) else 0
                target = int(settings.show_cache_per_cat)
                if cnt < target:
                    await ensure_cat_cache(nm, target, prefer_random=True)
            except Exception as e:
                log_action('show_cache_warm_error', nm, str(e))
            await asyncio.sleep(0.25)

    await asyncio.gather(*[_worker() for _ in range(concurrency)])
