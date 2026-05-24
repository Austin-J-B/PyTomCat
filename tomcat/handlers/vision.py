"""Computer-vision Discord commands (identify, crop, detect)."""

from __future__ import annotations
import os
import io
import asyncio
import aiohttp
import discord
from datetime import timezone
from typing import Dict, Any, Optional, List

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
_CV_TIMEOUT_SEC = max(6.0, float(getattr(settings, "cv_timeout_ms", 6000)) / 1000.0)
_CV_MODAL_TIMEOUT_SEC = max(
    _CV_TIMEOUT_SEC,
    float(getattr(settings, "cv_modal_timeout_ms", 30000)) / 1000.0,
)
_CV_COLD_TIMEOUT_SEC = 120.0  # generous timeout for first-time model loading


def _identify_timeout_sec() -> float:
    """Pick a per-call timeout based on the active backend.

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

#---------- background reaction listener ----------
async def _wait_for_top5_reaction(
    client: Any,
    reply_msg: discord.Message,
    embed: discord.Embed,
    results: list,
) -> None:
    """Wait (in the background) for a user to click '?' then expand the embed."""
    try:
        def check(reaction, user):
            return (
                str(reaction.emoji) == "\u2753"
                and reaction.message.id == reply_msg.id
                and not user.bot
            )

        reaction, user = await client.wait_for("reaction_add", timeout=120.0, check=check)

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

    except asyncio.TimeoutError:
        pass
    except Exception as e:
        log_action("viz_reaction_wait_error", f"err={type(e).__name__}", str(e))


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

    tmp = []
    try:
        timeout = _CV_COLD_TIMEOUT_SEC if V._yolo is None else _CV_TIMEOUT_SEC

        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)

        async with _CV_SEM:
            out = await asyncio.wait_for(
                asyncio.to_thread(V.detect, data),
                timeout=timeout,
            )
        file = discord.File(io.BytesIO(out.boxed_jpeg), filename="detected.jpg")

        count = len(out.results)
        msg = f"Found {count} object{'s' if count != 1 else ''}."
        await ch.send(content=msg, file=file)

    except asyncio.TimeoutError:
        log_action("viz_detect_error", "err=TimeoutError", f"cap={timeout:.1f}s")
        await ch.send("Sorry, detection timed out. Try again in a moment.")
    except ValueError as ve:
        await ch.send(str(ve))
    except Exception as e:
        log_action("viz_detect_error", f"err={type(e).__name__}", str(e))
        await ch.send("Sorry, detection failed.")
    finally:
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

    tmp = []
    try:
        timeout = _CV_COLD_TIMEOUT_SEC if V._yolo is None else _CV_TIMEOUT_SEC

        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)

        async with _CV_SEM:
            out = await asyncio.wait_for(
                asyncio.to_thread(V.crop, data),
                timeout=timeout,
            )
        crop_bytes = list(getattr(out, "crops", []) or [])
        if crop_bytes:
            files = [
                discord.File(io.BytesIO(crop_bytes[i]), filename=f"crop_{i + 1}.jpg")
                for i in range(len(crop_bytes))
            ]
            if len(files) <= 10:
                content = "Cropped view:" if len(files) == 1 else f"Cropped views ({len(files)} cats):"
                await ch.send(content=content, files=files)
            else:
                for start in range(0, len(files), 10):
                    batch = files[start:start + 10]
                    if start == 0:
                        content = f"Cropped views ({len(files)} cats):"
                    else:
                        content = None
                    await ch.send(content=content, files=batch)
        else:
            file = discord.File(io.BytesIO(out.boxed_jpeg), filename="crop.jpg")
            await ch.send(content="Cropped view:", file=file)

    except asyncio.TimeoutError:
        log_action("viz_crop_error", "err=TimeoutError", f"cap={timeout:.1f}s")
        await ch.send("Sorry, crop timed out. Try again in a moment.")
    except ValueError as ve:
        await ch.send(str(ve))
    except Exception as e:
        log_action("viz_crop_error", f"err={type(e).__name__}", str(e))
        await ch.send("Sorry, crop failed.")
    finally:
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
    try:
        from ..vision.backend import notify_modal_activity
        await notify_modal_activity()
    except Exception as e:
        log_action("modal_keep_warm_notify_error", f"err={type(e).__name__}", str(e))

    tmp = []
    reply_msg: Optional[discord.Message] = None
    heads_up: Optional[asyncio.Task] = None
    try:
        timeout = _identify_timeout_sec()
        reply_msg = await ch.send("Processing image...")
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)

        # If the call takes longer than ~3s, the Modal container is almost
        # certainly cold-starting (warm calls return in ~1-2s). Edit the
        # placeholder so the user knows the wait is normal, not a hang.
        async def _cold_start_notice():
            try:
                await asyncio.sleep(3.0)
                await reply_msg.edit(
                    content="Booting up CV models (~15-20s on the first request after a quiet period)..."
                )
            except (asyncio.CancelledError, Exception):
                pass
        heads_up = asyncio.create_task(_cold_start_notice())

        async with _CV_SEM:
            out = await asyncio.wait_for(
                asyncio.to_thread(V.identify, data),
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

        # ACTIVE LISTENER FOR '?' REACTION
        # Spawn as a background task so the handler returns immediately and
        # doesn't hold the dispatch chain alive for up to 2 minutes.
        client = ctx.get("client")
        if not client and message.guild:
            client = message.guild.me._state._get_client()

        if client:
            asyncio.create_task(
                _wait_for_top5_reaction(client, reply_msg, embed, out.results)
            )

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
