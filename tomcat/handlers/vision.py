# tomcat/handlers/vision.py
from __future__ import annotations
import os
import io
import zipfile
import asyncio
import aiohttp
import discord
from typing import Dict, Any, Optional, List

from ..config import settings
from ..logger import log_action
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..intent_router import Intent  # type: ignore
from ..vision import vision as V

# ---------- helpers ----------
async def _download_attachment(att: discord.Attachment) -> str:
    # Size gate before download
    if att.size and settings.cv_max_download_mb and (att.size > settings.cv_max_download_mb * 1024 * 1024):
        raise ValueError(f"Attachment too large ({att.size} bytes). Max {settings.cv_max_download_mb} MB.")
    os.makedirs(settings.cv_temp_dir, exist_ok=True)
    path = os.path.join(settings.cv_temp_dir, f"{att.id}_{att.filename}")
    async with aiohttp.ClientSession() as sess:
        async with sess.get(att.url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    with open(path, "wb") as f:
        f.write(data)
    return path

def _first_image(message: discord.Message) -> Optional[discord.Attachment]:
    # Prefer image attachments in this message; then check referenced message if any
    for a in getattr(message, "attachments", []) or []:
        if (a.content_type or "").startswith("image/"):
            return a
    ref = getattr(message, "reference", None)
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        for a in getattr(ref.resolved, "attachments", []) or []:
            if (a.content_type or "").startswith("image/"):
                return a
    return None

async def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

async def _cleanup(paths: List[str]):
    for p in paths:
        try:
            os.remove(p)
        except Exception:
            pass

# ---------- public handlers ----------
async def handle_cv_detect(intent: 'Intent', ctx: Dict[str, Any]) -> None:
    message: discord.Message = ctx["message"]
    ch: discord.abc.MessageableChannel = ctx["channel"]

    att = _first_image(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to an image, then say `TomCat, detect`.")
        return

    tmp = []
    try:
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        boxed = await asyncio.to_thread(V.detect, data)

        file = discord.File(io.BytesIO(boxed), filename="detected.jpg")
        emb = discord.Embed(
            color=0x2F3136,  # same slate-gray as the other embeds
        )
        emb.set_image(url="attachment://detected.jpg")
        await ch.send(embed=emb, file=file)


    except ValueError as ve:
        await ch.send(str(ve))
    except Exception as e:
        log_action("viz_detect_error", f"err={type(e).__name__}", str(e))
        await ch.send("Sorry, detection failed.")
    finally:
        await _cleanup(tmp)

async def handle_cv_crop(intent: 'Intent', ctx: Dict[str, Any]) -> None:
    message: discord.Message = ctx["message"]
    ch: discord.abc.MessageableChannel = ctx["channel"]

    att = _first_image(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to an image, then say `TomCat, crop`.")
        return

    tmp = []
    try:
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        crops = await asyncio.to_thread(V.crop, data)

        if not crops:
            await ch.send("No cats detected.")
            return

        if len(crops) == 1:
            file = discord.File(io.BytesIO(crops[0]), filename="crop.jpg")
            emb = discord.Embed(
                title="Cropped photo",
                color=0x2F3136
            )
            emb.set_image(url="attachment://crop.jpg")
            await ch.send(embed=emb, file=file)
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for i, b in enumerate(crops, start=1):
                    z.writestr(f"crop_{i}.jpg", b)
            buf.seek(0)
            await ch.send("Multiple cats detected. Here are the crops:", file=discord.File(buf, filename="crops.zip"))

    except ValueError as ve:
        await ch.send(str(ve))
    except Exception as e:
        log_action("viz_crop_error", f"err={type(e).__name__}", str(e))
        await ch.send("Sorry, crop failed.")
    finally:
        await _cleanup(tmp)

async def handle_cv_identify(intent: 'Intent', ctx: Dict[str, Any]) -> None:
    message: discord.Message = ctx["message"]
    ch: discord.abc.MessageableChannel = ctx["channel"]

    att = _first_image(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to an image, then say `TomCat, identify`.")
        return

    tmp = []
    reply_msg: Optional[discord.Message] = None
    try:
        reply_msg = await ch.send("Processing image…")
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        out = await asyncio.to_thread(V.identify, data)

        # build embed w/ results, keep v5.6 vibe
        lines = []
        for r in out.results:
            name = r["name"]
            conf = r["conf"]
            idx = r["index"]
            lines.append(f"{idx}. **{name}** ({conf*100:.1f}%)")

        desc = ("".join(lines) if lines else "_no classifier configured_")
        emb = discord.Embed(
            description=desc,
            color=0x2F3136
        )
        emb.set_image(url="attachment://identified.jpg")
        file = discord.File(io.BytesIO(out.boxed_jpeg), filename="identified.jpg")

        await reply_msg.edit(content=None, attachments=[file], embed=emb)
        try:
            await reply_msg.add_reaction("✅")
            await reply_msg.add_reaction("❌")
        except Exception:
            pass

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
        await _cleanup(tmp)
