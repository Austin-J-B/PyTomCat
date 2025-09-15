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
    build_profile_embed as _build_profile_embed,
)
from ..logger import log_action 
import re
import os, io, asyncio, aiohttp
from ..vision import vision as V
from ..services.show_cache import pop_one_cached, ensure_cat_cache, latest_cached_bytes
from pathlib import Path
from typing import Optional
from ..config import settings
from ..services import profile_cache as PC




def _display_name(full: str) -> str:
    """Drop leading 'ID. ' from names like '1. Microwave'."""
    return re.sub(r"^\s*\d+\.\s*", "", str(full or "")).strip()


class PhotoView(discord.ui.View):
    def __init__(self, cat_name: str):
        super().__init__(timeout=None)  # no expiry while the bot is running
        self.cat_name = cat_name

    @discord.ui.button(label="Show me another", style=discord.ButtonStyle.primary)
    async def another(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer immediately to avoid 3s interaction timeout
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass
        # Try cache first for speed
        cached_bytes, meta = await pop_one_cached(self.cat_name)
        if cached_bytes:
            ex = set()
            try:
                if isinstance(meta, dict) and meta.get('serial'):
                    import re as _re
                    ex.add(_re.sub(r"[^0-9]", "", str(meta['serial'])))
            except Exception:
                pass
            asyncio.create_task(ensure_cat_cache(self.cat_name, settings.show_cache_per_cat, exclude_serials=ex))
            display = _display_name(self.cat_name)
            title = f"__**Random Photo of {display}**__"
            serial = meta.get('serial','Unknown') if isinstance(meta, dict) else 'Unknown'
            rev = meta.get('reverse_index','?') if isinstance(meta, dict) else '?'
            tot = meta.get('total_available','?') if isinstance(meta, dict) else '?'
            desc = (
                f"**Here's a random photo of {display}**\n"
                f"(Photo {rev} out of {tot})\n"
                f"Image: {serial}"
            )
            e2 = discord.Embed(title=title, description=desc, color=0x2F3136)
            file = discord.File(io.BytesIO(cached_bytes), filename="cache.jpg")
            e2.set_image(url="attachment://cache.jpg")
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=e2, attachments=[file], view=self)
            return
        # Else pull a random recent photo
        pick2 = await get_random_photo(self.cat_name)
        if isinstance(pick2, str):
            await interaction.followup.edit_message(message_id=interaction.message.id, content=pick2, embed=None, attachments=[], view=None)
            return
        full = self.cat_name
        display = _display_name(full)
        title = f"__**Random Photo of {display}**__"
        desc = (
            f"**Here's a random photo of {display}**\n"
            f"(Photo {pick2.get('reverse_index','?')} out of {pick2.get('total_available','?')})\n"
            f"Image: {pick2.get('serial','Unknown')}"
        )
        e2 = discord.Embed(title=title, description=desc, color=0x2F3136)
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
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=e2, attachments=[file], view=self)
        else:
            if img_url:
                e2.set_image(url=img_url)
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=e2, attachments=[], view=self)


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

    # Try cached photo first for speed without hitting Sheets
    cached_bytes: Optional[bytes] = None
    cached_meta = None
    cached_bytes, cached_meta = await pop_one_cached(name, use_sheet=False)
    if cached_bytes:
        # Send immediate embed; enrich from cached sidecar profile if available (no live sheet)
        display = _display_name(cached_meta.get('display_name') or name) if isinstance(cached_meta, dict) else _display_name(name)
        title = f"__**Random Photo of {display}**__"
        desc = f"**Here's a random photo of {display}**\n(Photo {cached_meta.get('reverse_index','?')} out of {cached_meta.get('total_available','?')})\nImage: {cached_meta.get('serial','Unknown')}" if isinstance(cached_meta, dict) else None
        embed = discord.Embed(title=title, description=desc or "", color=0x2F3136)
        # No extra profile fields in the caption
        file = discord.File(io.BytesIO(cached_bytes), filename="cache.jpg")
        embed.set_image(url="attachment://cache.jpg")
        await ch.send(embed=embed, file=file)

        # Refill cache in background (exclude served serial)
        async def _refill():
            ex = set()
            try:
                if isinstance(cached_meta, dict) and cached_meta.get('serial'):
                    import re as _re
                    ex.add(_re.sub(r"[^0-9]", "", str(cached_meta['serial'])))
            except Exception:
                pass
            try:
                asyncio.create_task(ensure_cat_cache(cached_meta.get('full_name') or name, settings.show_cache_per_cat, exclude_serials=ex))
            except Exception:
                pass
        asyncio.create_task(_refill())
        return

    # Fallback: fetch a random recent photo and send simple caption
    pick = await get_random_photo(name)
    if isinstance(pick, str):
        # Try resolving via profile, then retry once with actual name
        profile = await get_cat_profile(name)
        if isinstance(profile, dict):
            pick = await get_random_photo(profile.get('actual_name') or name)
        if isinstance(pick, str):
            await ch.send(pick)
            return
    display = _display_name(pick.get('actual_name') or name)
    title = f"__**Random Photo of {display}**__"
    desc = (
        f"**Here's a random photo of {display}**\n"
        f"(Photo {pick.get('reverse_index','?')} out of {pick.get('total_available','?')})\n"
        f"Image: {pick.get('serial','Unknown')}"
    )
    embed = discord.Embed(title=title, description=desc, color=0x2F3136)
    img_url = pick.get('url')
    img_bytes_for_embed: Optional[bytes] = None
    tmp: Optional[str] = None
    if settings.auto_crop_show_photo and img_url:
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
        embed.set_image(url="attachment://crop.jpg")
        await ch.send(embed=embed, file=file)
    else:
        if img_url:
            embed.set_image(url=img_url)
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
        await ch.send("Which cat? Ex: `TomCat, show me Microwave`")
        return

    # Try cache first without hitting Sheets
    cached_bytes2, cached_meta2 = await pop_one_cached(name, use_sheet=False)
    if cached_bytes2:
        ex2 = set()
        try:
            if isinstance(cached_meta2, dict) and cached_meta2.get('serial'):
                import re as _re
                ex2.add(_re.sub(r"[^0-9]", "", str(cached_meta2['serial'])))
        except Exception:
            pass
        asyncio.create_task(ensure_cat_cache(cached_meta2.get('full_name') if isinstance(cached_meta2, dict) else name, settings.show_cache_per_cat, exclude_serials=ex2))
        img_url = None
        img_bytes_for_embed: Optional[bytes] = cached_bytes2
        tmp: Optional[str] = None
        total_avail = cached_meta2.get('total_available','?') if isinstance(cached_meta2, dict) else '?'
        reverse_idx = cached_meta2.get('reverse_index','?') if isinstance(cached_meta2, dict) else '?'
        serial = cached_meta2.get('serial','cached') if isinstance(cached_meta2, dict) else 'cached'
        display = _display_name(cached_meta2.get('display_name') or name) if isinstance(cached_meta2, dict) else _display_name(name)
    else:
        # Fallback to sheet-backed path
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
    if cached_bytes2:
        desc = (
            f"**Here's a random photo of {display}**\n"
            f"(Photo {reverse_idx} out of {total_avail})\n"
            f"Image: {serial}"
        )
    else:
        desc = (
            f"**Here's a random photo of {display}**\n"
            f"(Photo {pick.get('reverse_index','?')} out of {pick.get('total_available','?')})\n"
            f"Image: {pick.get('serial','Unknown')}"
        )
    embed = discord.Embed(title=title, description=desc, color=0x2F3136)

    if img_bytes_for_embed:
        file = discord.File(io.BytesIO(img_bytes_for_embed), filename="crop.jpg")
        embed.set_image(url="attachment://crop.jpg")
        # Pass FULL_NAME: use cached_meta's full_name when cached, else actual
        full_for_button = (cached_meta2.get('full_name') if isinstance(cached_meta2, dict) else None) or (actual if not cached_bytes2 else name)
        await ch.send(embed=embed, file=file, view=PhotoView(full_for_button))
    else:
        if img_url:
            embed.set_image(url=img_url)
        await ch.send(embed=embed, view=PhotoView(actual))



# Optional: tiny wrapper to expose a strict "who is" alias if you want a separate name
async def handle_cat_profile(intent: 'Intent', ctx: dict) -> None:
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Which cat? Ex: `TomCat, who is Microwave`")
        return
    # Prefer cached profile snapshot to avoid live sheet; fall back to sheet builder
    prof = PC.get_profile(name)
    if not prof:
        emb = await _build_profile_embed(name)
        if isinstance(emb, str):
            await ch.send(emb)
            return
        try:
            e = discord.Embed.from_dict(emb)
        except Exception:
            title = emb.get('title') if isinstance(emb, dict) else f"__**{name}**__"
            e = discord.Embed(title=title or f"__**{name}**__", color=0x2F3136)
            for f in (emb.get('fields') or []):
                try:
                    e.add_field(name=f.get('name'), value=f.get('value'), inline=f.get('inline', False))
                except Exception:
                    continue
            img = emb.get('image', {}).get('url') if isinstance(emb.get('image'), dict) else None
            if img:
                e.set_image(url=img)
        await ch.send(embed=e)
        return
    # Build embed from cached profile with classic text layout
    actual = prof.get('actual_name') or name
    display = re.sub(r"^\s*\d+\.\s*", "", str(actual))
    e = discord.Embed(title=f"__**{display}**__", color=0x2F3136)
    lines = []

    desc = prof.get("physical_description") or prof.get("physical") or None
    if desc:
        lines.append(f"**Description:** {desc}")
    beh = prof.get("behavior")
    if beh:
        lines.append(f"**Behavior:** {beh}")
    loc = prof.get("location")
    if loc:
        lines.append(f"**Location:** {loc}")
    age = prof.get("birthday_estimate") or prof.get("age")
    if age:
        lines.append(f"**Age Estimate:** {age}")
    sex = prof.get("sex")
    if sex:
        lines.append(f"**Sex:** {sex}")
    tnrd = prof.get("tnrd")
    if tnrd:
        lines.append(f"**TNR Status:** {tnrd}")
    tnd = prof.get("tnr_date")
    if tnd:
        lines.append(f"**TNR Date:** {tnd}")
    last_bits = []
    if prof.get("last_seen_date"): last_bits.append(str(prof["last_seen_date"]))
    if prof.get("last_seen_time"): last_bits.append(str(prof["last_seen_time"]))
    if prof.get("last_seen_by"):   last_bits.append(f"by {prof['last_seen_by']}")
    if last_bits:
        lines.append("**Last Reported:** " + " ".join(last_bits))
    nicks = prof.get("nicknames")
    if nicks:
        lines.append(f"**Common Nicknames:** {nicks}")
    comm = prof.get("comments")
    if comm:
        lines.append(f"**Comments:** {comm}")
    e.description = "\n".join(lines)
    # Image: prefer most-recent cached JPEG to avoid Sheets; else profile image_url; else skip
    img_bytes = latest_cached_bytes(actual) or None
    if img_bytes:
        file = discord.File(io.BytesIO(img_bytes), filename="recent.jpg")
        e.set_image(url="attachment://recent.jpg")
        await ch.send(embed=e, file=file)
        return
    img_url = prof.get("image_url")
    if img_url:
        e.set_image(url=img_url)
    await ch.send(embed=e)
