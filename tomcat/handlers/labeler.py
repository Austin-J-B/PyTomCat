"""API endpoints for the web-based image labeling tool.

Routes:
  GET  /api/labeler/queue/detect    - Serials needing detector labels
  GET  /api/labeler/queue/classify  - Serials needing classifier labels
  GET  /api/labeler/image/<sn>      - Image + existing annotations
  GET  /api/labeler/cached_image/<sn> - Cached image bytes (fast)
  POST /api/labeler/detect          - Run YOLO+SAM → boxes
  POST /api/labeler/identify        - Run DINOv3 → top-N candidates
  POST /api/labeler/save            - Batch save annotations to sheet
  GET  /api/labeler/cats            - List all cat names for dropdown
"""
from __future__ import annotations
import io
import re
import os
import asyncio
import aiohttp
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from aiohttp import web

from ..config import settings
from ..logger import log_action
from ..vision import vision as V
from ..services.catsheets import get_tcb_pics_rows
from ..services.sheets_client import sheets_client
from ..services import labeler_cache

#Column indices in TCB Pics Formatted (0-indexed)
COL_CAT_ID = 0       #A: CatID (e.g., "1. Twix")
COL_URL = 6          #G: Picture Link
COL_SERIAL = 7       #H: Serial number
COL_BOX_COORDS = 8   #I: BoxCoordinates
COL_BOX_CAT_IDS = 9  #J: BoxCatIDs

#Regex for serial extraction
SN_PATTERN = re.compile(r"sn(\d+)", re.IGNORECASE)
_IDENTIFY_CONCURRENCY = max(1, int(os.getenv("LABELER_IDENTIFY_CONCURRENCY", "2") or "2"))
_IDENTIFY_TIMEOUT_SEC = float(os.getenv("LABELER_IDENTIFY_TIMEOUT_SEC", "45") or "45")
_IDENTIFY_PREFETCH_TIMEOUT_SEC = float(os.getenv("LABELER_IDENTIFY_PREFETCH_TIMEOUT_SEC", "20") or "20")
_identify_sem = asyncio.Semaphore(_IDENTIFY_CONCURRENCY)


def _parse_serial(val: str) -> Optional[int]:
    """Parse serial from string like 'sn1234' or just '1234'."""
    m = SN_PATTERN.search(val)
    if m:
        return int(m.group(1))
    if val.strip().isdigit():
        return int(val.strip())
    return None


def _with_cors(resp: web.Response, request: web.Request) -> web.Response:
    """Add CORS headers to response."""
    origin = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


#---------- Queue Endpoints ----------

async def get_queue_detect(request: web.Request) -> web.Response:
    """Return list of serials needing detector labels (empty BoxCoordinates)."""
    try:
        rows = get_tcb_pics_rows(ttl_sec=60)
        queue = []
        for row in rows[1:]:  #Skip header
            if len(row) <= COL_SERIAL:
                continue
            sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if sn is None:
                continue
            box_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
            if not box_coords.strip():
                url = row[COL_URL] if len(row) > COL_URL else ""
                if url.startswith("http"):
                    queue.append({"serial": sn, "url": url})
        total = len(queue)
        #Trigger background cache fill for first 15 images
        if queue:
            asyncio.create_task(labeler_cache.ensure_cache_filled(queue[:15]))
        return _with_cors(web.json_response({"queue": queue[:500], "total": total}), request)
    except Exception as e:
        log_action("labeler_queue_detect_error", "error", str(e))
        return _with_cors(web.Response(status=500, text="Internal server error"), request)


async def get_queue_classify(request: web.Request) -> web.Response:
    """Return serials with boxes but incomplete cat IDs."""
    try:
        rows = get_tcb_pics_rows(ttl_sec=60)
        queue = []
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if sn is None:
                continue
            box_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
            box_cat_ids = row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else ""
            
            #Skip if no boxes, rejected, or empty
            if not box_coords.strip() or box_coords.strip().lower() == "rejected":
                continue
            
            #Count boxes vs labels
            num_boxes = len(box_coords.split("|"))
            labels = box_cat_ids.split("|") if box_cat_ids else []
            num_labeled = sum(1 for lbl in labels if lbl.strip())
            
            if num_labeled < num_boxes:
                url = row[COL_URL] if len(row) > COL_URL else ""
                if url.startswith("http"):
                    queue.append({
                        "serial": sn,
                        "url": url,
                        "boxes": box_coords,
                        "labels": box_cat_ids,
                        "num_boxes": num_boxes,
                        "num_labeled": num_labeled,
                    })
        total = len(queue)
        #Trigger background cache fill for first 15 images
        if queue:
            asyncio.create_task(labeler_cache.ensure_cache_filled(queue[:15]))
        return _with_cors(web.json_response({"queue": queue[:500], "total": total}), request)
    except Exception as e:
        log_action("labeler_queue_classify_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_image(request: web.Request) -> web.Response:
    """Get image data and annotations for a specific serial."""
    try:
        sn_str = request.match_info.get("sn", "")
        sn = _parse_serial(sn_str)
        if sn is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)
        
        rows = get_tcb_pics_rows(ttl_sec=60)
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if row_sn == sn:
                return _with_cors(web.json_response({
                    "serial": sn,
                    "url": row[COL_URL] if len(row) > COL_URL else "",
                    "cat_id": row[COL_CAT_ID] if len(row) > COL_CAT_ID else "",
                    "box_coords": row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else "",
                    "box_cat_ids": row[COL_BOX_CAT_IDS] if len(row) > COL_BOX_CAT_IDS else "",
                }), request)
        
        return _with_cors(web.Response(status=404, text="Serial not found"), request)
    except Exception as e:
        log_action("labeler_get_image_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_cached_image(request: web.Request) -> web.Response:
    """Get cached image bytes for a serial. Downloads on-demand if not cached."""
    try:
        sn_str = request.match_info.get("sn", "")
        sn = _parse_serial(sn_str)
        if sn is None:
            return _with_cors(web.Response(status=400, text="Invalid serial"), request)
        
        #Try cache first
        data = labeler_cache.get_cached_image(sn)
        if data:
            resp = web.Response(body=data, content_type="image/jpeg")
            return _with_cors(resp, request)
        
        #Not cached - look up URL and download directly
        rows = get_tcb_pics_rows(ttl_sec=60)
        url = None
        for row in rows[1:]:
            if len(row) <= COL_SERIAL:
                continue
            row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
            if row_sn == sn:
                url = row[COL_URL] if len(row) > COL_URL else ""
                break
        
        if not url or not url.startswith("http"):
            return _with_cors(web.Response(status=404, text="Image URL not found"), request)
        
        #Download and cache
        data = await labeler_cache.get_or_download(sn, url)
        if not data:
            return _with_cors(web.Response(status=502, text="Failed to download image"), request)
        
        resp = web.Response(body=data, content_type="image/jpeg")
        return _with_cors(resp, request)
    except Exception as e:
        log_action("labeler_cached_image_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- CV Endpoints ----------

async def post_detect(request: web.Request) -> web.Response:
    """Run YOLO+SAM detection on an image. Accepts serial (preferred) or URL."""
    try:
        data = await request.json()
        serial = data.get("serial")
        url = data.get("url")
        fast = bool(data.get("fast"))
        
        image_bytes = None
        
        #Try cache first if serial provided
        if serial:
            image_bytes = labeler_cache.get_cached_image(int(serial))

        #If serial provided but not cached and no URL, look up URL by serial
        if serial and not image_bytes and not url:
            rows = get_tcb_pics_rows(ttl_sec=60)
            for row in rows[1:]:
                if len(row) <= COL_SERIAL:
                    continue
                row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if row_sn == int(serial):
                    url = row[COL_URL] if len(row) > COL_URL else ""
                    break
        
        #Fall back to URL download
        if not image_bytes and url:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    resp.raise_for_status()
                    image_bytes = await resp.read()
        
        if not image_bytes:
            return _with_cors(web.Response(status=400, text="No image available"), request)
        
        #Run detection (fast mode skips SAM)
        if fast:
            result = await asyncio.to_thread(V.detect, image_bytes)
        else:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(V.detect_with_sam, image_bytes), timeout=25)
            except Exception:
                #Fallback to YOLO-only if SAM fails or times out
                result = await asyncio.to_thread(V.detect, image_bytes)
        
        #Encode boxed image as base64
        import base64
        boxed_b64 = base64.b64encode(result.boxed_jpeg).decode("ascii") if result.boxed_jpeg else ""
        
        #Convert boxes to YOLO normalized format (cx, cy, w, h)
        #detect_with_sam returns absolute coords (x1,y1,x2,y2), need to normalize
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        iw, ih = img.size
        
        yolo_boxes = []
        raw_boxes = getattr(result, "boxes", None) or []
        if not raw_boxes and getattr(result, "results", None):
            raw_boxes = [r.get("box") for r in result.results if r.get("box")]
        for (x1, y1, x2, y2) in raw_boxes:
            cx = (x1 + x2) / 2 / iw
            cy = (y1 + y2) / 2 / ih
            w = (x2 - x1) / iw
            h = (y2 - y1) / ih
            yolo_boxes.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        
        return _with_cors(web.json_response({
            "boxed_image": boxed_b64,
            "boxes": yolo_boxes,
            "boxes_yolo": "|".join(yolo_boxes),
        }), request)
    except Exception as e:
        log_action("labeler_detect_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def post_refine(request: web.Request) -> web.Response:
    """Refine provided boxes using SAM."""
    try:
        data = await request.json()
        serial = data.get("serial")
        url = data.get("url")
        boxes_raw = data.get("boxes", [])
        passes = int(data.get("passes") or 1)

        image_bytes = None
        if serial:
            image_bytes = labeler_cache.get_cached_image(int(serial))

        if serial and not image_bytes and not url:
            rows = get_tcb_pics_rows(ttl_sec=60)
            for row in rows[1:]:
                if len(row) <= COL_SERIAL:
                    continue
                row_sn = _parse_serial(row[COL_SERIAL] if len(row) > COL_SERIAL else "")
                if row_sn == int(serial):
                    url = row[COL_URL] if len(row) > COL_URL else ""
                    break

        if not image_bytes and url:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    resp.raise_for_status()
                    image_bytes = await resp.read()

        if not image_bytes:
            return _with_cors(web.Response(status=400, text="No image available"), request)

        boxes: List[Tuple[float, float, float, float]] = []
        for b in boxes_raw:
            try:
                parts = [float(p) for p in str(b).strip().split()]
            except Exception:
                continue
            if len(parts) == 4:
                boxes.append((parts[0], parts[1], parts[2], parts[3]))

        try:
            refined = await asyncio.wait_for(
                asyncio.to_thread(V.refine_boxes, image_bytes, boxes, passes=passes),
                timeout=25,
            )
        except Exception:
            #Fallback to original boxes if SAM refine fails or times out
            from PIL import Image
            img = Image.open(io.BytesIO(image_bytes))
            iw, ih = img.size
            refined = []
            for (cx, cy, w, h) in boxes:
                x1 = (cx - w / 2) * iw
                y1 = (cy - h / 2) * ih
                x2 = (cx + w / 2) * iw
                y2 = (cy + h / 2) * ih
                refined.append((x1, y1, x2, y2))

        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        iw, ih = img.size
        yolo_boxes = []
        for (x1, y1, x2, y2) in refined:
            cx = (x1 + x2) / 2 / iw
            cy = (y1 + y2) / 2 / ih
            w = (x2 - x1) / iw
            h = (y2 - y1) / ih
            yolo_boxes.append(f"{cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

        return _with_cors(web.json_response({
            "boxes": yolo_boxes,
            "boxes_yolo": "|".join(yolo_boxes),
        }), request)
    except Exception as e:
        log_action("labeler_refine_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def post_identify(request: web.Request) -> web.Response:
    """Run DINOv3 identification on crops from an image."""
    try:
        data = await request.json()
        serial = data.get("serial")
        url = data.get("url")
        prefetch = bool(data.get("prefetch"))
        boxes_raw = data.get("boxes", [])  #List of "cx cy w h" strings

        image_bytes = None
        if serial:
            try:
                image_bytes = labeler_cache.get_cached_image(int(serial))
            except Exception:
                image_bytes = None

        if not image_bytes and not url:
            return _with_cors(web.Response(status=400, text="Missing url or serial"), request)

        if not image_bytes and url:
            #Download image
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    resp.raise_for_status()
                    image_bytes = await resp.read()

        if not image_bytes:
            return _with_cors(web.Response(status=400, text="No image available"), request)

        boxes: List[Tuple[float, float, float, float]] = []
        for b in boxes_raw:
            try:
                parts = [float(p) for p in str(b).strip().split()]
            except Exception:
                continue
            if len(parts) == 4:
                boxes.append((parts[0], parts[1], parts[2], parts[3]))

        #Run identify on provided boxes (normalized cx,cy,w,h).
        #Prefetch requests should never monopolize worker capacity.
        acquired = False
        try:
            if prefetch:
                try:
                    await asyncio.wait_for(_identify_sem.acquire(), timeout=0.05)
                    acquired = True
                except asyncio.TimeoutError:
                    return _with_cors(web.Response(status=429, text="Busy"), request)
            else:
                await _identify_sem.acquire()
                acquired = True

            timeout_sec = _IDENTIFY_PREFETCH_TIMEOUT_SEC if prefetch else _IDENTIFY_TIMEOUT_SEC
            result = await asyncio.wait_for(
                asyncio.to_thread(V.identify_boxes, image_bytes, boxes),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            log_action("labeler_identify_timeout", f"serial={serial}", f"prefetch={prefetch}")
            return _with_cors(web.Response(status=504, text="Identify timed out"), request)
        finally:
            if acquired:
                _identify_sem.release()

        #Enrich candidates with physical descriptions from local cache (if available)
        try:
            from ..services import profile_cache
            for crop in result.results:
                for cand in crop.get("candidates", []) or []:
                    name = cand.get("name")
                    if not name:
                        continue
                    prof = profile_cache.get_profile_local(str(name))
                    if not prof:
                        continue
                    desc = prof.get("physical_description") or prof.get("physical")
                    if desc:
                        cand["desc"] = str(desc)
        except Exception:
            pass

        return _with_cors(web.json_response({"results": result.results}), request)
    except Exception as e:
        log_action("labeler_identify_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- Save Endpoint ----------

async def post_save(request: web.Request) -> web.Response:
    """Batch save annotations to the sheet."""
    try:
        data = await request.json()
        updates = data.get("updates", [])  #List of {serial, box_coords, box_cat_ids}
        
        if not updates:
            return _with_cors(web.Response(status=400, text="No updates"), request)
        
        #Get sheet
        gc = sheets_client()
        sh = gc.open_by_key(settings.sheet_catabase_id)
        ws = sh.worksheet("TCB Pics Formatted")
        
        #Build serial -> row index mapping
        rows = ws.get_all_values()
        serial_to_row = {}
        for idx, row in enumerate(rows[1:], start=2):  #1-indexed, skip header
            if len(row) > COL_SERIAL:
                sn = _parse_serial(row[COL_SERIAL])
                if sn is not None:
                    serial_to_row[sn] = idx
        
        #Build cell updates
        import time
        cells_to_update = []
        for upd in updates:
            sn = upd.get("serial")
            if sn not in serial_to_row:
                continue
            row_num = serial_to_row[sn]
            if "box_coords" in upd:
                cells_to_update.append({
                    "range": f"I{row_num}",
                    "values": [[upd["box_coords"]]]
                })
            if "box_cat_ids" in upd:
                cells_to_update.append({
                    "range": f"J{row_num}",
                    "values": [[upd["box_cat_ids"]]]
                })
        
        #Batch update with throttling
        chunk_size = 50
        for i in range(0, len(cells_to_update), chunk_size):
            chunk = cells_to_update[i:i + chunk_size]
            ws.batch_update(chunk)
            if i + chunk_size < len(cells_to_update):
                time.sleep(1)
        
        log_action("labeler_save", "saved", f"{len(updates)} annotations")
        return _with_cors(web.json_response({
            "status": "ok",
            "saved": len(updates),
        }), request)
    except Exception as e:
        log_action("labeler_save_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- Cat List Endpoint ----------

async def get_cats(request: web.Request) -> web.Response:
    """Return list of all known cat names from the gallery."""
    try:
        cats = await asyncio.to_thread(V.get_all_cats)
        return _with_cors(web.json_response({"cats": cats}), request)
    except Exception as e:
        log_action("labeler_get_cats_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- Reference Cache Endpoints ----------

async def post_refs_warm(request: web.Request) -> web.Response:
    """Warm the per-cat reference cache for classifier refs."""
    try:
        force = False
        try:
            body = await request.json()
            force = bool(body.get("force"))
        except Exception:
            force = False
        status = await V.warm_labeler_refs(force=force)
        return _with_cors(web.json_response(status), request)
    except Exception as e:
        log_action("labeler_refs_warm_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


async def get_refs_status(request: web.Request) -> web.Response:
    """Get reference cache status."""
    try:
        return _with_cors(web.json_response(V.labeler_ref_status()), request)
    except Exception as e:
        log_action("labeler_refs_status_error", "error", str(e))
        return _with_cors(web.Response(status=500, text=str(e)), request)


#---------- OPTIONS handlers for CORS ----------

async def options_handler(request: web.Request) -> web.Response:
    """Handle CORS preflight requests."""
    resp = web.Response(status=204)
    return _with_cors(resp, request)


#---------- Route registration ----------

def get_labeler_routes() -> List:
    """Return list of labeler API routes for registration in main.py."""
    return [
        web.get("/api/labeler/queue/detect", get_queue_detect),
        web.get("/api/labeler/queue/classify", get_queue_classify),
        web.get("/api/labeler/image/{sn}", get_image),
        web.get("/api/labeler/cached_image/{sn}", get_cached_image),
        web.post("/api/labeler/detect", post_detect),
        web.post("/api/labeler/refine", post_refine),
        web.post("/api/labeler/identify", post_identify),
        web.post("/api/labeler/save", post_save),
        web.get("/api/labeler/cats", get_cats),
        web.post("/api/labeler/refs/warm", post_refs_warm),
        web.get("/api/labeler/refs/status", get_refs_status),
        #CORS preflight
        web.options("/api/labeler/queue/detect", options_handler),
        web.options("/api/labeler/queue/classify", options_handler),
        web.options("/api/labeler/image/{sn}", options_handler),
        web.options("/api/labeler/cached_image/{sn}", options_handler),
        web.options("/api/labeler/detect", options_handler),
        web.options("/api/labeler/refine", options_handler),
        web.options("/api/labeler/identify", options_handler),
        web.options("/api/labeler/save", options_handler),
        web.options("/api/labeler/cats", options_handler),
        web.options("/api/labeler/refs/warm", options_handler),
        web.options("/api/labeler/refs/status", options_handler),
    ]
