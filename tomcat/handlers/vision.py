"""Computer-vision Discord commands (identify, crop, detect)."""

from __future__ import annotations
import os
import io
import asyncio
import aiohttp
import discord
from typing import Dict, Any, Optional, List

from ..config import settings
from ..logger import log_action
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..intent_router import Intent
from ..vision import vision as V

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

def _first_image(message: discord.Message) -> Optional[discord.Attachment]:
    """Pick the first image attachment from a message if any."""
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
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        
        out = await asyncio.to_thread(V.detect, data)
        file = discord.File(io.BytesIO(out.boxed_jpeg), filename="detected.jpg")
        
        count = len(out.results)
        msg = f"Found {count} object{'s' if count != 1 else ''}."
        await ch.send(content=msg, file=file)

    except ValueError as ve:
        await ch.send(str(ve))
    except Exception as e:
        log_action("viz_detect_error", f"err={type(e).__name__}", str(e))
        await ch.send("Sorry, detection failed.")
    finally:
        await _cleanup(tmp)

async def handle_cv_crop(intent: 'Intent', ctx: Dict[str, Any]) -> None:
    """Crop detected cats and send the result (collage)."""
    message: discord.Message = ctx["message"]
    ch: discord.abc.MessageableChannel = ctx["channel"]

    att = _first_image(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to one, then say `TomCat, crop`.")
        return

    tmp = []
    try:
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        
        out = await asyncio.to_thread(V.crop, data)
        file = discord.File(io.BytesIO(out.boxed_jpeg), filename="crop.jpg")
        
        await ch.send(content="Cropped view:", file=file)

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

    att = _first_image(message)
    if not att:
        if not ctx.get("silent_on_no_image"):
            await ch.send("Attach an image or reply to one, then say `TomCat, identify`.")
        return

    tmp = []
    reply_msg: Optional[discord.Message] = None
    try:
        reply_msg = await ch.send("Processing image...")
        path = await _download_attachment(att); tmp.append(path)
        data = await _read_bytes(path)
        out = await asyncio.to_thread(V.identify, data)

        # Build initial Description
        lines = []
        for r in out.results:
            name = r["name"]
            conf = r["conf"]
            idx = r["index"]
            lines.append(f"{idx}. **{name}** ({conf*100:.1f}%)")

        desc = ("\n".join(lines) if lines else "_no cat detected_")
        
        embed = discord.Embed(
            description=desc,
            color=0x2F3136
        )
        # INSTRUCTIONAL FOOTER
        if out.results:
            embed.set_footer(text="Was I right? React ✅/❌. React ❓ to see top 5 guesses.")
        
        embed.set_image(url="attachment://identified.jpg")
        file = discord.File(io.BytesIO(out.boxed_jpeg), filename="identified.jpg")

        await reply_msg.edit(content=None, attachments=[file], embed=embed)
        
        if not out.results:
            return

        # Add Reactions
        try:
            await reply_msg.add_reaction("✅")
            await reply_msg.add_reaction("❌")
            await reply_msg.add_reaction("❓")
        except Exception:
            pass
        
        # ACTIVE LISTENER FOR '?' REACTION
        # We try to get the bot client from the message context to wait for a reaction.
        # This allows the "Top 5" feature to work without a database.
        try:
            client = ctx.get("client")
            # Fallback if client wasn't passed directly in ctx
            if not client and message.guild:
                client = message.guild.me._state._get_client()
            
            if client:
                def check(reaction, user):
                    return (
                        str(reaction.emoji) == "❓" 
                        and reaction.message.id == reply_msg.id 
                        and not user.bot
                    )
                
                # Wait up to 2 minutes for someone to ask "Who else could it be?"
                reaction, user = await client.wait_for("reaction_add", timeout=120.0, check=check)
                
                # If we get here, someone clicked '?' - Update the Embed!
                expanded_lines = []
                for r in out.results:
                    idx = r["index"]
                    top5 = r.get("top5", [])
                    expanded_lines.append(f"**Cat #{idx} Candidates:**")
                    for rank, (c_name, c_conf) in enumerate(top5):
                        expanded_lines.append(f"`{rank+1}.` {c_name} ({c_conf*100:.1f}%)")
                    expanded_lines.append("") # Spacer
                
                new_desc = "\n".join(expanded_lines)
                embed.description = new_desc
                embed.set_footer(text="Showing Top 5 Candidates")
                await reply_msg.edit(embed=embed)
                
        except asyncio.TimeoutError:
            # No one clicked '?' in time, just stop listening.
            pass
        except Exception as e:
            # Reaction listening isn't critical, so don't crash if permissions fail
            log_action("viz_reaction_wait_error", f"err={type(e).__name__}", str(e))

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