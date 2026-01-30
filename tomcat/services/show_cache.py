"""Local filesystem cache for show-photo responses."""

from __future__ import annotations
import os, re, io, asyncio
import ipaddress
from typing import Optional, List, Tuple
from pathlib import Path
import aiohttp
import time
from urllib.parse import urlparse

from ..config import settings
from ..logger import log_action
from ..vision import vision as V
from PIL import Image
from .catsheets import get_most_recent_photo, get_cat_profile, get_tcb_pics_rows
from .catsheets import sheets_client  #type: ignore


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
    """Load the newest cached JPEG bytes for a cat if present."""
    """Return bytes of the most recent cached JPG for this cat (highest sn), or None if missing."""
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

async def _download_bytes(url: str, timeout_sec: float = 6.0) -> Optional[bytes]:
    """Fetch raw bytes from a show-photo URL with a timeout."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if host:
            if host.lower() == "localhost" or host.lower().endswith(".local"):
                return None
            try:
                ip = ipaddress.ip_address(host)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return None
            except ValueError:
                pass
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()
    except Exception:  #network/parse error; caller handles None
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

_RECENTPICS_ROWS: Optional[List[List[str]]] = None
_RECENTPICS_TS: float = 0.0

def _set_recentpics_rows(rows: Optional[List[List[str]]]) -> None:
    """Seed the in-memory view of RecentPics."""
    global _RECENTPICS_ROWS, _RECENTPICS_TS
    _RECENTPICS_ROWS = rows
    _RECENTPICS_TS = time.monotonic()


def reset_recentpics_cache() -> None:
    """Clear the cached RecentPics rows so the next call refetches from Sheets."""
    global _RECENTPICS_ROWS, _RECENTPICS_TS
    _RECENTPICS_ROWS = None
    _RECENTPICS_TS = 0.0

#--- INSERT IN tomcat/services/show_cache.py ---

async def list_recent_pairs(full_name: str) -> List[Tuple[str, str, int, int]]:
    """Return URL/SERIAL pairs from TCB Pics Formatted for a given FULL_NAME."""
    try:
        #Reuse the existing row caching mechanism, but fetch from the new sheet
        rows = None
        now = time.monotonic()
        ttl = max(1, int(getattr(settings, 'show_sheet_recentpics_ttl_sec', 300) or 300))
        
        if _RECENTPICS_ROWS is not None and (now - _RECENTPICS_TS) < ttl:
            rows = _RECENTPICS_ROWS
        
        if rows is None:
            rows = get_tcb_pics_rows(ttl)
            if rows:
                _set_recentpics_rows(rows)
        if not rows:
            return []

        #Normalize query
        key = re.sub(r"[^a-z0-9]+", "", (full_name or "").lower())
        
        #Columns in TCB Pics Formatted
        COL_LABEL = 0
        COL_URL = 6
        COL_SERIAL = 7

        matches = []
        #Skip header row (rows[1:])
        for r in rows[1:]:
            if len(r) > COL_SERIAL:
                #Check name match
                if re.sub(r"[^a-z0-9]+", "", (r[COL_LABEL] or "").lower()) == key:
                    if (r[COL_URL] or "").startswith("http"):
                        matches.append(r)

        if not matches:
            return []

        #Sort by serial; report reverse_index from the bottom so higher serials show larger numbers
        def parse_serial(row):
            try:
                return int(re.sub(r"\D", "", row[COL_SERIAL]) or 0)
            except:
                return 0
        
        matches.sort(key=parse_serial, reverse=True)
        
        total = len(matches)
        out: List[Tuple[str, str, int, int]] = []
        
        for idx, r in enumerate(matches):
            #(URL, Serial, Reverse Index, Total)
            out.append((
                r[COL_URL], 
                r[COL_SERIAL] or "0", 
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
    """Rename legacy catId_sn#### files to sn#### to mirror sheet naming."""
    if not os.path.isdir(cat_dir):
        return
    for fn in list(os.listdir(cat_dir)):
        renamed = False
        src = os.path.join(cat_dir, fn)

        #Case 1: legacy catId_sn1234.jpg -> sn1234.jpg
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
    """Guarantee at least min_count cached photos for a cat, downloading as needed."""
    """Ensure at least min_count images exist for the cat; returns total count after fill."""
    min_count = max(0, int(min_count or settings.show_cache_per_cat))
    cid = _cat_id_from_full(full_name)
    display_name = None
    if cid is None:
        prof = await get_cat_profile(full_name)
        if isinstance(prof, dict):
            actual = prof.get('actual_name') or ''
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
    #Always shuffle so new cache entries vary across the full history
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

    #Collect valid serials from current sheet data to detect stale cache entries
    valid_serials = set()
    for _, serial, _, _ in pairs:
        sn = re.sub(r"[^0-9]", "", serial or "")
        valid_serials.add(sn)

    #Prune ghost files: remove cached images whose serials no longer exist in sheet
    for sn in list(have_serials):
        if sn not in valid_serials and sn not in (exclude_serials or set()):
            base = f"sn{str(sn).zfill(4)}"
            try:
                p_jpg = os.path.join(cdir, f"{base}.jpg")
                p_json = os.path.join(cdir, f"{base}.json")
                if os.path.exists(p_jpg):
                    os.remove(p_jpg)
                if os.path.exists(p_json):
                    os.remove(p_json)
                have_serials.discard(sn)
                log_action('show_cache_prune_ghost', base, f"cat={cid}")
            except Exception as e:
                log_action('show_cache_prune_ghost_error', base, str(e))

    total = len(existing)
    #Reuse a single HTTP session for downloads to cut overhead
    timeout = aiohttp.ClientTimeout(total=8.0)
    headers = {"User-Agent": "TomCatShowCache/1.0 (+https://example.invalid)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
      for url, serial, reverse_index, total_available in pairs:
        #CHANGE 1: Do not break early! We need to scan existing files to update totals.
        #if total >= min_count: break 

        sn = re.sub(r"[^0-9]", "", serial or "") or "0"
        
        if sn in have_serials:
            #CHANGE 2: "Heal" stale metadata in existing files without re-downloading images
            try:
                base = f"sn{str(sn).zfill(4)}"
                jp = os.path.join(cdir, f"{base}.json")
                if os.path.exists(jp):
                    import json as _json
                    #Read current meta
                    meta = _json.loads(Path(jp).read_text(encoding='utf-8'))
                    
                    #If total count or index has drifted, update the file
                    if meta.get("total_available") != total_available:
                        meta["total_available"] = total_available
                        meta["reverse_index"] = reverse_index
                        Path(jp).write_text(_json.dumps(meta), encoding='utf-8')
            except Exception:
                pass
            continue

        #CHANGE 3: Only apply the limit when considering a NEW download
        if total >= min_count:
            continue

        #... (rest of download logic remains the same) ...
        #Download with shared session
        raw = None
        #Try a couple of times to download; some hosts are flaky
        for attempt in range(1, 4):
            try:
                async with sess.get(url) as resp:
                    resp.raise_for_status()
                    raw = await resp.read()
                if raw:
                    break
            except Exception as e:
                log_action('show_cache_download_fail', url, f"sn={sn} attempt={attempt} err={type(e).__name__}:{e}")
                raw = None
                await asyncio.sleep(0.15 * attempt)
        if not raw:
            continue
        #Optional crop during fill
        data = raw
        try:
            if bool(getattr(settings, 'show_cache_crop_on_fill', True)):
                cropped = _maybe_crop_single(raw)
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
            #Write sidecar JSON with metadata
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
    """Populate the alias index from RecentPics rows."""
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
                    disp = meta.get('display_name') or meta.get('full_name')
                    if disp:
                        _NAME_INDEX[_norm(disp)] = int(meta.get('cat_id') or int(sub))
                        break
                except Exception:
                    continue
        except Exception:
            continue


def rebuild_name_index() -> None:
    """Expose a public hook to refresh the cached name index."""
    _build_name_index()

def _fix_cached_reverse_index(meta: Optional[dict]) -> Optional[dict]:
    """Pass-through for backwards compatibility - indices are now stored correctly."""
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

async def pop_one_cached(full_name: str, use_sheet: bool = True) -> tuple[Optional[bytes], Optional[dict]]:
    """Return and remove one cached image entry, optionally refilling from Sheets."""
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
    if cid is None and use_sheet:
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
    #Pick a random cached file to improve variety
    try:
        import random as _rand
        path = _rand.choice(files)
    except Exception:
        path = sorted(files)[0]
    data = None
    try:
        data = Path(path).read_bytes()
    except Exception:
        pass
    #Load sidecar JSON
    meta: Optional[dict] = None
    try:
        base = os.path.splitext(path)[0]
        mp = base + ".json"
        if os.path.exists(mp):
            import json as _json
            meta = _fix_cached_reverse_index(_json.loads(Path(mp).read_text(encoding='utf-8')))
    except Exception:
        meta = None
    try:
        os.remove(path)
        #Also remove meta sidecar if present
        try:
            mp = os.path.splitext(path)[0] + ".json"
            if os.path.exists(mp):
                os.remove(mp)
        except Exception:
            pass
    except Exception:
        pass
    return data, meta

async def warm_cache_on_boot() -> None:
    """Background task that primes caches for frequently requested cats."""
    if not settings.show_cache_prefill_on_boot:
        return
    try:
        rows = get_tcb_pics_rows(getattr(settings, 'show_sheet_recentpics_ttl_sec', 300))
        if not rows:
            log_action('show_cache_warm_error', 'sheet', 'no rows fetched')
            return
    except Exception as e:
        log_action('show_cache_warm_error', 'sheet', str(e))
        return
    #Seed row cache so list_recent_pairs doesn't re-hit sheet immediately
    try:
        _set_recentpics_rows(rows)
    except Exception:
        pass
    names = []
    for r in rows[1:]:
        full = (r[0] if r else '').strip()
        if full:
            names.append(full)
    if settings.show_cache_warm_limit > 0:
        names = names[:settings.show_cache_warm_limit]

    sem = asyncio.Semaphore(max(1, settings.show_cache_warm_concurrency))

    async def _one(nm: str):
        async with sem:
            cdir = _cache_dir_for(_cat_id_from_full(nm) or 0)
            cnt = len([p for p in os.listdir(cdir) if p.lower().endswith('.jpg')]) if os.path.isdir(cdir) else 0
            target = int(settings.show_cache_per_cat)
            if cnt < target:
                try:
                    await ensure_cat_cache(nm, target, prefer_random=True)
                except Exception as e:
                    log_action('show_cache_warm_error', nm, str(e))
            await asyncio.sleep(0.25)

    await asyncio.gather(*[_one(n) for n in names])
