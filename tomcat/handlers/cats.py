from __future__ import annotations
import discord
from typing import Any
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..intent_router import Intent  # type: ignore
from ..services.catsheets import (
    get_cat_profile,
    get_recent_photo as get_random_photo,
    get_most_recent_photo as get_latest_photo,
)
from ..logger import log_action 
import re
import os, io, asyncio, aiohttp
from ..vision import vision as V
from pathlib import Path
from typing import Optional
from ..config import settings




def _display_name(full: str) -> str:
    """Drop leading 'ID. ' from names like '1. Microwave'."""
    return re.sub(r"^\s*\d+\.\s*", "", str(full or "")).strip()

class PhotoView(discord.ui.View):
    def __init__(self, cat_name: str):
        super().__init__(timeout=None)  # no expiry while the bot is running
        self.cat_name = cat_name

    @discord.ui.button(label="Show me another", style=discord.ButtonStyle.primary)
    async def another(self, interaction: discord.Interaction, button: discord.ui.Button):
        # self.cat_name is the FULL_NAME from CatDatabase (e.g., "2. Twix").
        pick2 = await get_random_photo(self.cat_name)
        if isinstance(pick2, str):
            await interaction.response.edit_message(content=pick2, embed=None, attachments=[], view=None)
            return
        # Build embed like the original random photo style
        full = self.cat_name
        display = _display_name(full)
        title = f"__**Random Photo of {display}**__"
        desc = (
            f"**Here's a random photo of {display}**\n"
            f"(Photo {pick2.get('reverse_index','?')} out of {pick2.get('total_available','?')})\n"
            f"Image: {pick2.get('serial','Unknown')}"
        )
        e2 = discord.Embed(title=title, description=desc, color=0x2F3136)

        # Try single-cat crop with timeout
        img_bytes_for_embed: Optional[bytes] = None
        tmp: Optional[str] = None
        img_url = pick2.get("url")
        if img_url:
            try:
                tmp = await _download_to_temp(img_url, settings.cv_temp_dir)
                raw = Path(tmp).read_bytes()
                def _crop_once(raw_bytes: bytes) -> Optional[bytes]:
                    crops = V.crop(raw_bytes)
                    return crops[0] if len(crops) == 1 else None
                img_bytes_for_embed = await asyncio.wait_for(
                    asyncio.to_thread(_crop_once, raw), timeout=(settings.cv_timeout_ms / 1000.0)
                )
            except Exception:
                img_bytes_for_embed = None
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

        if img_bytes_for_embed:
            file = discord.File(io.BytesIO(img_bytes_for_embed), filename="crop.jpg")
            e2.set_image(url="attachment://crop.jpg")
            await interaction.response.edit_message(embed=e2, attachments=[file], view=self)
        else:
            if img_url:
                e2.set_image(url=img_url)
            await interaction.response.edit_message(embed=e2, attachments=[], view=self)


def _add_field(embed: discord.Embed, name: str, value: Any, inline: bool = True) -> None:
    if value is None:
        return
    s = str(value).strip()
    if not s:
        return
    embed.add_field(name=name, value=s[:1024], inline=inline)

async def handle_cat_show(intent: 'Intent', ctx: dict) -> None:
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Who am I showing? Try: `TomCat, show Microwave`")
        return

    profile = await get_cat_profile(name)
    if isinstance(profile, str):
        await ch.send(profile)
        return
    actual = profile["actual_name"]
    display = _display_name(actual)

    # Prefer catabase image; else fall back to most recent from RecentPics
    img_url = profile.get("image_url")
    if not (isinstance(img_url, str) and img_url.startswith("http")):
        recent = await get_latest_photo(actual)
        if isinstance(recent, dict) and recent.get("url"):
            img_url = recent["url"]

    # Try fast auto-crop (get bytes first; we’ll decide how to send after building embed)
    cropped_bytes: Optional[bytes] = None
    if settings.auto_crop_show_photo and img_url:
        tmp = None
        try:
            tmp = await _download_to_temp(img_url, settings.cv_temp_dir)
            raw = Path(tmp).read_bytes()

            def _crop_once(raw_bytes: bytes) -> Optional[bytes]:
                crops = V.crop(raw_bytes)
                if len(crops) == 1:
                    return crops[0]
                return None

            cropped_bytes = await asyncio.wait_for(
                asyncio.to_thread(_crop_once, raw), timeout=(settings.cv_timeout_ms / 1000.0)
            )
        except Exception:
            cropped_bytes = None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    physical = profile.get("physical_description") or "Unknown"
    behavior = profile.get("behavior") or "Unknown"
    location = profile.get("location") or "Unknown"
    age = profile.get("age") or "Unknown"
    tnrd = profile.get("tnrd") or "Unknown"
    nick = profile.get("nicknames")
    last_date = profile.get("last_seen_date") or "Unknown"
    last_time = profile.get("last_seen_time") or "Unknown"
    last_by = profile.get("last_seen_by") or "Unknown"

    description = (
        f"**Description:** {physical}\n"
        f"**Behavior:** {behavior}\n"
        f"**Location:** {location}\n"
        f"**Age Estimate:** {age}\n"
        f"**TNR Status:** {tnrd}"
        + (f"\n**Common Nicknames:** {nick}" if nick else "")
        + f"\n**Last Reported:** {last_date} at {last_time} by {last_by}"
    ).strip()

    embed = discord.Embed(
        title=f"__**{display}**__",
        description=description,
        color=0x2F3136,
    )

    if cropped_bytes:
        file = discord.File(io.BytesIO(cropped_bytes), filename="crop.jpg")
        embed.set_image(url="attachment://crop.jpg")
        log_action("handle_cat_show", f"name={actual}", "sending embed (cropped)")
        await ch.send(embed=embed, file=file)
    else:
        if img_url:
            embed.set_image(url=img_url)
        log_action("handle_cat_show", f"name={actual}", "sending embed")
        await ch.send(embed=embed)



async def _download_to_temp(url: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    fname = url.split("?")[0].split("/")[-1] or "photo.jpg"
    path = os.path.join(dest_dir, f"show_{hash(url)}_{fname}")
    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    with open(path, "wb") as f:
        f.write(data)
    return path


async def handle_cat_photo(intent: 'Intent', ctx: dict) -> None:
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Who am I showing? Try: `TomCat, show me Microwave`")
        return

    profile = await get_cat_profile(name)
    if isinstance(profile, str):
        await ch.send(profile)
        return
    actual = profile["actual_name"]
    display = _display_name(actual)

    pick = await get_random_photo(actual)
    if isinstance(pick, str):
        await ch.send(pick)
        return

    img_url = pick.get("url")
    img_bytes_for_embed: Optional[bytes] = None
    tmp: Optional[str] = None

    # Try fast auto-crop if enabled
    if settings.auto_crop_show_photo and img_url:
        try:
            tmp = await _download_to_temp(img_url, settings.cv_temp_dir)
            raw = Path(tmp).read_bytes()

            def _crop_once(raw_bytes: bytes) -> Optional[bytes]:
                crops = V.crop(raw_bytes)
                if len(crops) == 1:
                    return crops[0]
                return None

            img_bytes_for_embed = await asyncio.wait_for(
                asyncio.to_thread(_crop_once, raw), timeout=(settings.cv_timeout_ms / 1000.0)
            )
        except Exception:
            img_bytes_for_embed = None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    title = f"__**Random Photo of {display}**__"
    desc = (
        f"**Here's a random photo of {display}**\n"
        f"(Photo {pick.get('reverse_index','?')} out of {pick.get('total_available','?')})\n"
        f"Image: {pick.get('serial','Unknown')}"
    )
    embed = discord.Embed(title=title, description=desc, color=0x2F3136)

    if img_bytes_for_embed:
        file = discord.File(io.BytesIO(img_bytes_for_embed), filename="crop.jpg")
        embed.set_image(url="attachment://crop.jpg")
        # Pass FULL_NAME (actual) so the button can fetch correctly from RecentPics
        await ch.send(embed=embed, file=file, view=PhotoView(actual))
    else:
        if img_url:
            embed.set_image(url=img_url)
        await ch.send(embed=embed, view=PhotoView(actual))



# Optional: tiny wrapper to expose a strict "who is" alias if you want a separate name
async def handle_cat_profile(intent: 'Intent', ctx: dict) -> None:
    # Reuse your existing bio handler
    await handle_cat_show(intent, ctx)
