"""Computer-vision Discord commands (identify, crop, detect)."""

from __future__ import annotations
import os
import io
import asyncio
import concurrent.futures
import time
import aiohttp
import discord
from datetime import timezone
from typing import Dict, Any, Optional, List, Tuple

from ..config import settings
from ..logger import log_action
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..intent_router import Intent
from ..vision import vision as V
from ..services.vision_feedback import register_identify_feedback

# Serialize all CV operations through a single semaphore.  YOLO's predict()
# and the DINOv3 encoder mutate internal state and are not thread-safe; running
# concurrent asyncio.to_thread(V.*) calls on the shared module-level models
# causes deadlocks that permanently freeze the handler coroutine.
_CV_SEM = asyncio.Semaphore(1)

# Dedicated single-worker executor for V.detect/V.crop/V.identify. Using the
# default asyncio executor lets an orphaned CV thread (e.g. Modal RPC stuck
# past wait_for's timeout — the underlying thread has no asyncio-aware cancel)
# steal a worker from sheet sync, Gmail polling, and the labeler. With
# max_workers=1, a second CV call queues here instead, and wait_for still
# times out cleanly — so the fallout from one stuck call is bounded.
_CV_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="tc-cv"
)
_CV_TIMEOUT_SEC = max(6.0, float(getattr(settings, "cv_timeout_ms", 6000)) / 1000.0)
_CV_MODAL_TIMEOUT_SEC = max(
    _CV_TIMEOUT_SEC,
    float(getattr(settings, "cv_modal_timeout_ms", 60000)) / 1000.0,
)
_CV_COLD_TIMEOUT_SEC = 120.0  # generous timeout for first-time model loading


async def _run_cv(fn, *args):
    """Run a blocking CV function on the dedicated single-worker executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CV_EXECUTOR, fn, *args)


def _cv_timeout_sec() -> float:
    """Pick a per-call timeout based on the active backend. Shared by
    identify / detect / crop so all three handle cold starts the same way.

    Local: warm path is sub-second; 6s is generous.
    Modal: cold path (snapshot restore + GPU move + first inference) can take
    a few seconds; warm path is well under 1s. Use the modal-specific floor
    so the first call after a scaledown doesn't strand the user.
    Cold (models not yet loaded): always allow 120s on first-ever call.
    """
    from ..vision.backend import LocalBackend, get_backend

    backend = get_backend()
    if not isinstance(backend, LocalBackend):
        return _CV_MODAL_TIMEOUT_SEC
    if V._yolo is None or V._clf is None:
        return _CV_COLD_TIMEOUT_SEC
    return _CV_TIMEOUT_SEC


# Backwards-compatible alias.
_identify_timeout_sec = _cv_timeout_sec


def _spawn_cold_start_notice(reply_msg: "discord.Message") -> "asyncio.Task":
    """Edit a placeholder reply to a friendly 'booting CV' note if the call
    runs longer than ~3s.

    Warm CV calls return in ~1-2s and this task is cancelled before it fires,
    so warm users never see it. On a cold Modal container (or first-ever local
    load) the wait is normal, not a hang — this tells the user that. Used by
    identify, detect, and crop so cold starts look the same across all three
    instead of crop/detect sitting silent. Edit failures are swallowed (UX
    polish, not load-bearing).
    """
    async def _notice() -> None:
        try:
            await asyncio.sleep(3.0)
            await reply_msg.edit(
                content="Booting up CV models (~15-20s on the first request after a quiet period)..."
            )
        except (asyncio.CancelledError, Exception):
            pass
    return asyncio.create_task(_notice())


async def _notify_modal_activity_safe() -> None:
    """Bump the Modal keep-warm window. No-op on local backend / keep-warm off."""
    try:
        from ..vision.backend import notify_modal_activity
        await notify_modal_activity()
    except Exception as e:
        log_action("modal_keep_warm_notify_error", f"err={type(e).__name__}", str(e))


async def _edit_or_send(
    reply_msg: Optional["discord.Message"],
    ch: Any,
    content: str,
) -> None:
    """Land a result/error on the placeholder reply if we have one (clearing any
    attachments/embed), else send a fresh message. Falls back to a plain send if
    the edit fails (e.g. message deleted)."""
    try:
        if reply_msg is not None:
            await reply_msg.edit(content=content, attachments=[], embed=None)
            return
    except Exception:
        pass
    try:
        await ch.send(content)
    except Exception:
        pass

#---------- helpers ----------
async def _download_attachment(att: discord.Attachment) -> str:
    """Save a Discord attachment locally and return the temp path."""
    if att.size and settings.cv_max_download_mb and (att.size > settings.cv_max_download_mb * 1024 * 1024):
        raise ValueError(f"Attachment too large ({att.size} bytes). Max {settings.cv_max_download_mb} MB.")
    os.makedirs(settings.cv_temp_dir, exist_ok=True)
    safe_name = os.path.basename(att.filename or "attachment")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c in {".", "_", "-"}).strip("._-") or "attachment"
    path = os.path.join(settings.cv_temp_dir, f"{att.id}_{safe_name}")
    async with aiohttp.ClientSession() as sess:
        async with sess.get(att.url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    with open(path, "wb") as f:
        f.write(data)
    return path

def _first_image_with_source(message: discord.Message) -> tuple[Optional[discord.Attachment], Optional[discord.Message]]:
    """Pick the first image attachment and the message that owns it."""
    for a in getattr(message, "attachments", []) or []:
        if (a.content_type or "").startswith("image/"):
            return a, message
    ref = getattr(message, "reference", None)
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        for a in getattr(ref.resolved, "attachments", []) or []:
            if (a.content_type or "").startswith("image/"):
                return a, ref.resolved
    return None, None


def _first_image(message: discord.Message) -> Optional[discord.Attachment]:
    att, _ = _first_image_with_source(message)
    return att

async def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

async def _cleanup(paths: List[str]):
    for p in paths:
        try:
            os.remove(p)
        except Exception:
            pass


def _format_confidence_pct(score: Any) -> str:
    try:
        value = float(score)
    except Exception:
        value = 0.0
    value = max(0.0, min(1.0, value))
    return f"{value * 100:.1f}%"

#---------- top-5 reaction registry ----------
# When handle_cv_identify produces results, it registers the embed + results
# here keyed on the reply message id. on_raw_reaction_add dispatches the \u2753
# emoji to handle_top5_reaction below, which expands the embed.
#
# This replaces the older client.wait_for("reaction_add", ...) approach,
# which silently no-op'd whenever the reply message had aged out of
# discord.py's in-memory message cache (default 1000 entries). raw_reaction_add
# fires for any reaction regardless of cache state.
_TOP5_TTL_SEC = 1800  # 30 min \u2014 gallery retrain pending TTL is much longer
_top5_listeners: Dict[int, Tuple[float, discord.Embed, list, discord.Message]] = {}


def _gc_top5_listeners(now: float) -> None:
    expired = [mid for mid, entry in _top5_listeners.items() if now - entry[0] > _TOP5_TTL_SEC]
    for mid in expired:
        _top5_listeners.pop(mid, None)


def _register_top5_listener(reply_msg: discord.Message, embed: discord.Embed, results: list) -> None:
    """Remember an identify reply so a later \u2753 react can expand the top-5."""
    now = time.monotonic()
    _gc_top5_listeners(now)
    _top5_listeners[int(reply_msg.id)] = (now, embed, list(results), reply_msg)


async def handle_top5_reaction(reply_message_id: int) -> bool:
    """Expand the embed for a registered identify reply. Returns True if handled.

    Called from main.on_raw_reaction_add when a \u2753 reaction is observed.
    """
    entry = _top5_listeners.pop(int(reply_message_id), None)
    if not entry:
        return False
    _, embed, results, reply_msg = entry
    try:
        expanded_lines = []
        for r in results:
            idx = r["index"]
            top5 = r.get("top5", [])
            expanded_lines.append(f"**Cat #{idx} Candidates:**")
            for rank, (c_name, c_conf) in enumerate(top5):
                expanded_lines.append(f"`{rank+1}.` {c_name} ({_format_confidence_pct(c_conf)})")
            expanded_lines.append("")
        embed.description = "\n".join(expanded_lines)
        embed.set_footer(text="Showing Top 5 Candidates")
        await reply_msg.edit(embed=embed)
    except Exception as e:
        log_action("viz_top5_edit_error", f"err={type(e).__name__}", str(e))
    return True


#---------- public handlers ----------
async def handle_cv_detect(intent: 'Intent', ctx: Dict[str, Any]) -> None:
    """Run object detection on an image and report bounding boxes."""
    message: discord.Message = ctx["message"]
    ch: discord.abc.MessageableChannel = ctx["channel"]

    att = _first_image(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to one, then say `TomCat, detect`.")
        return

    # detect/crop also hit the GPU — keep the Modal container warm like identify.
    await _notify_modal_activity_safe()

    tmp = []
    reply_msg: Optional[discord.Message] = None
    heads_up: Optional[asyncio.Task] = None
    try:
        timeout = _cv_timeout_sec()
        # Send a placeholder immediately so the request is visibly acknowledged
        # even while an earlier CV op is still holding the semaphore / cold-loading.
        reply_msg = await ch.send("Processing image...")
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        heads_up = _spawn_cold_start_notice(reply_msg)

        async with _CV_SEM:
            out = await asyncio.wait_for(
                _run_cv(V.detect, data),
                timeout=timeout,
            )
        if heads_up is not None and not heads_up.done():
            heads_up.cancel()

        file = discord.File(io.BytesIO(out.boxed_jpeg), filename="detected.jpg")
        count = len(out.results)
        msg = f"Found {count} object{'s' if count != 1 else ''}."
        await reply_msg.edit(content=msg, attachments=[file])

    except asyncio.TimeoutError:
        log_action("viz_detect_error", "err=TimeoutError", f"cap={timeout:.1f}s")
        await _edit_or_send(reply_msg, ch, "Sorry, detection timed out. Try again in a moment.")
    except ValueError as ve:
        await _edit_or_send(reply_msg, ch, str(ve))
    except Exception as e:
        log_action("viz_detect_error", f"err={type(e).__name__}", str(e))
        await _edit_or_send(reply_msg, ch, "Sorry, detection failed.")
    finally:
        if heads_up is not None and not heads_up.done():
            heads_up.cancel()
        await _cleanup(tmp)

async def handle_cv_crop(intent: 'Intent', ctx: Dict[str, Any]) -> None:
    """Crop detected cats and send each crop as a separate attachment."""
    message: discord.Message = ctx["message"]
    ch: discord.abc.MessageableChannel = ctx["channel"]

    att = _first_image(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to one, then say `TomCat, crop`.")
        return

    # detect/crop also hit the GPU — keep the Modal container warm like identify.
    await _notify_modal_activity_safe()

    tmp = []
    reply_msg: Optional[discord.Message] = None
    heads_up: Optional[asyncio.Task] = None
    try:
        timeout = _cv_timeout_sec()
        reply_msg = await ch.send("Processing image...")
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        heads_up = _spawn_cold_start_notice(reply_msg)

        async with _CV_SEM:
            out = await asyncio.wait_for(
                _run_cv(V.crop, data),
                timeout=timeout,
            )
        if heads_up is not None and not heads_up.done():
            heads_up.cancel()

        crop_bytes = list(getattr(out, "crops", []) or [])
        if crop_bytes:
            files = [
                discord.File(io.BytesIO(crop_bytes[i]), filename=f"crop_{i + 1}.jpg")
                for i in range(len(crop_bytes))
            ]
            content = "Cropped view:" if len(files) == 1 else f"Cropped views ({len(files)} cats):"
            # Discord allows <=10 attachments per message: put the first batch on
            # the placeholder, send any overflow as follow-up messages.
            await reply_msg.edit(content=content, attachments=files[:10])
            for start in range(10, len(files), 10):
                await ch.send(files=files[start:start + 10])
        else:
            file = discord.File(io.BytesIO(out.boxed_jpeg), filename="crop.jpg")
            await reply_msg.edit(content="Cropped view:", attachments=[file])

    except asyncio.TimeoutError:
        log_action("viz_crop_error", "err=TimeoutError", f"cap={timeout:.1f}s")
        await _edit_or_send(reply_msg, ch, "Sorry, crop timed out. Try again in a moment.")
    except ValueError as ve:
        await _edit_or_send(reply_msg, ch, str(ve))
    except Exception as e:
        log_action("viz_crop_error", f"err={type(e).__name__}", str(e))
        await _edit_or_send(reply_msg, ch, "Sorry, crop failed.")
    finally:
        if heads_up is not None and not heads_up.done():
            heads_up.cancel()
        await _cleanup(tmp)

async def handle_cv_identify(intent: 'Intent', ctx: Dict[str, Any]) -> None:
    """Identify which known cat appears in an uploaded photo."""
    message: discord.Message = ctx["message"]
    ch: discord.abc.MessageableChannel = ctx["channel"]

    att, source_msg = _first_image_with_source(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to one, then say `TomCat, identify`.")
        return

    # Bump the Modal keep-warm window on every identify request. No-op when
    # CV_BACKEND=local or when keep-warm is disabled in settings.
    await _notify_modal_activity_safe()

    tmp = []
    reply_msg: Optional[discord.Message] = None
    heads_up: Optional[asyncio.Task] = None
    try:
        timeout = _cv_timeout_sec()
        reply_msg = await ch.send("Processing image...")
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        heads_up = _spawn_cold_start_notice(reply_msg)

        async with _CV_SEM:
            out = await asyncio.wait_for(
                _run_cv(V.identify, data),
                timeout=timeout,
            )

        # Cancel the cold-start notice immediately on success so it can't
        # race with the embed edit below. The finally block handles error paths.
        if heads_up is not None and not heads_up.done():
            heads_up.cancel()

        # Build initial Description
        lines = []
        for r in out.results:
            name = r["name"]
            conf = r["conf"]
            idx = r["index"]
            lines.append(f"{idx}. **{name}** ({_format_confidence_pct(conf)})")

        desc = ("\n".join(lines) if lines else "_no cat detected_")
        
        embed = discord.Embed(
            description=desc,
            color=0x2F3136
        )
        # INSTRUCTIONAL FOOTER
        if out.results:
            embed.set_footer(text="Was I right? React \u2705/\u274c. React \u2753 to see top 5 guesses.")
        
        embed.set_image(url="attachment://identified.jpg")
        file = discord.File(io.BytesIO(out.boxed_jpeg), filename="identified.jpg")

        await reply_msg.edit(content=None, attachments=[file], embed=embed)
        if out.results:
            try:
                await reply_msg.add_reaction("\u2705")
                await reply_msg.add_reaction("\u274c")
                await reply_msg.add_reaction("\u2753")
            except Exception:
                pass
        try:
            await asyncio.to_thread(
                register_identify_feedback,
                reply_message_id=int(reply_msg.id),
                reply_channel_id=int(getattr(ch, "id", 0) or 0),
                source_message_id=int(getattr(source_msg or message, "id", 0) or 0),
                source_channel_id=int(getattr(getattr(source_msg or message, "channel", None), "id", 0) or 0),
                guild_id=int(getattr(getattr(source_msg or message, "guild", None), "id", 0) or 0),
                image_bytes=data,
                results=list(out.results or []),
                source_image_url=str(getattr(att, "url", "") or ""),
                source_author_id=str(getattr(getattr(source_msg or message, "author", None), "id", "") or ""),
                source_username=str(getattr(getattr(source_msg, "author", None), "name", "") or ""),
                source_created_at=str(
                    (
                        getattr(source_msg, "created_at", None) or message.created_at
                    ).astimezone(timezone.utc).isoformat()
                ),
                source_filename=str(getattr(att, "filename", "") or ""),
                source_content_type=str(getattr(att, "content_type", "") or ""),
            )
        except Exception as e:
            log_action("viz_feedback_register_error", f"msg={getattr(reply_msg, 'id', 0)}", str(e))
        
        if not out.results:
            return

        # Register for '?' reaction dispatch (see _register_top5_listener).
        # Replaces an earlier client.wait_for() background task that silently
        # no-op'd whenever reply_msg had aged out of discord.py's message cache.
        _register_top5_listener(reply_msg, embed, out.results)

    except asyncio.TimeoutError:
        log_action("viz_identify_error", "err=TimeoutError", f"cap={timeout:.1f}s")
        if reply_msg:
            await reply_msg.edit(content="Sorry, identify timed out. Try again in a moment.", attachments=[], embed=None)
        else:
            await ch.send("Sorry, identify timed out. Try again in a moment.")
    except ValueError as ve:
        if reply_msg:
            await reply_msg.edit(content=str(ve), attachments=[], embed=None)
        else:
            await ch.send(str(ve))
    except Exception as e:
        log_action("viz_identify_error", f"err={type(e).__name__}", str(e))
        if reply_msg:
            await reply_msg.edit(content="Sorry, identify failed.", attachments=[], embed=None)
        else:
            await ch.send("Sorry, identify failed.")
    finally:
        # Cancel the cold-start notice if still pending so it doesn't race
        # with the success/error edit and overwrite the user-visible result.
        if heads_up is not None and not heads_up.done():
            heads_up.cancel()
        await _cleanup(tmp)
