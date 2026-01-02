"""Handlers that answer cat lookup requests and manage show-photo embeds."""

from __future__ import annotations
import discord
from typing import Any
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..intent_router import Intent  #type: ignore
from ..aliases import resolve_station_or_cat
from ..services.catsheets import (
    get_cat_profile,
    get_recent_photo as get_random_photo,
    get_most_recent_photo as get_latest_photo,
    build_profile_embed as _build_profile_embed,
)
from ..logger import log_action, log_event 
import re
import os, io, asyncio, aiohttp
import ipaddress
from urllib.parse import urlparse
from datetime import datetime, timezone
from ..vision import vision as V
from ..services.show_cache import pop_one_cached, ensure_cat_cache, latest_cached_bytes
from pathlib import Path
from typing import Optional
from ..config import settings
from ..services import profile_cache as PC
from discord.errors import NotFound




def _display_name(full: str) -> str:
    """Drop leading 'ID. ' from names like '1. Microwave'."""
    return re.sub(r"^\s*\d+\.\s*", "", str(full or "")).strip()


def _single_display_name(label: str, requested: str | None = None) -> str:
    """
    When a photo label lists multiple cats (e.g., '46. Princess, 45. Boots'),
    return just the requested cat's display name if present; otherwise the first.
    """
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    req_clean = _norm(_display_name(requested or "") or "")
    parts = [p.strip() for p in str(label or "").split(",") if p.strip()]
    for p in parts:
        disp = _display_name(p)
        if req_clean and _norm(disp) == req_clean:
            return disp
    if parts:
        return _display_name(parts[0])
    return _display_name(label or requested or "")


def _format_age_value(raw) -> str:
    """Convert various birthday/age representations into friendly text.

    - If provided an integer/float: interpret as years (with <1 treated as months).
    - If provided a date string: compute the age using today's date.
    - Otherwise return the original text.
    """
    if raw in (None, ""):
        return ""

    def _months_to_text(months: int) -> str:
        if months <= 0:
            return "Less than 1 month"
        if months == 1:
            return "1 month"
        return f"{months} months"

    def _years_to_text(years: int) -> str:
        if years == 1:
            return "1 year"
        return f"{years} years"

    #Numeric values: treat as approximate years
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value < 1:
            months = max(1, int(round(value * 12)))
            return _months_to_text(months)
        years = int(round(value))
        years = max(1, years)
        return _years_to_text(years)

    text = str(raw).strip()
    if not text:
        return ""

    #Parse date strings (mm/dd/yyyy, yyyy-mm-dd, etc.)
    parsed = None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed_dt = datetime.strptime(text, fmt)
            parsed = parsed_dt.date()
            break
        except Exception:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text).date()
        except Exception:
            parsed = None

    if parsed is None:
        return text

    today = datetime.now(timezone.utc).date()
    if parsed > today:
        return text

    #Compute months difference
    months_total = (today.year - parsed.year) * 12 + (today.month - parsed.month)
    if today.day < parsed.day:
        months_total -= 1

    if months_total <= 0:
        return "Less than 1 month"
    if months_total < 12:
        return _months_to_text(months_total)

    years = months_total // 12
    return _years_to_text(years)


class PhotoView(discord.ui.View):
    """Reusable view that lets users page through cached cat photos."""
    def __init__(self, cat_name: str):
        super().__init__(timeout=None)  #no expiry while the bot is running
        self.cat_name = cat_name

    async def _edit_or_send(
        self,
        interaction: discord.Interaction,
        *,
        embed: Optional[discord.Embed] = None,
        content: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        filename: str = "cache.jpg",
    ) -> tuple[str, int | None]:
        """Edit the original message when possible, else send a follow-up copy.

        Returns (delivery_mode, resulting_message_id).
        """
        attachments = []
        if image_bytes is not None:
            attachments = [discord.File(io.BytesIO(image_bytes), filename=filename)]
        try:
            await interaction.followup.edit_message(
                message_id=interaction.message.id,
                content=content,
                embed=embed,
                attachments=attachments,
                view=self,
            )
            return "edit", interaction.message.id
        except NotFound:
            files = None
            if image_bytes is not None:
                files = [discord.File(io.BytesIO(image_bytes), filename=filename)]
            sent = await interaction.followup.send(
                content=content,
                embed=embed,
                files=files,
                view=self,
            )
            sent_id = getattr(sent, "id", None)
            return "send", sent_id

    @discord.ui.button(label="Show me another", style=discord.ButtonStyle.primary)
    async def another(self, interaction: discord.Interaction, button: discord.ui.Button):
        #Defer immediately to avoid 3s interaction timeout
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass
        #Try cache first for speed
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
            display = _single_display_name(self.cat_name, self.cat_name)
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
            e2.set_image(url="attachment://cache.jpg")
            delivery, delivered_id = await self._edit_or_send(
                interaction,
                embed=e2,
                image_bytes=cached_bytes,
                filename="cache.jpg",
            )
            log_event({
                "event": "show_photo_page",
                "cat": self.cat_name,
                "source": "cache",
                "serial": str(serial),
                "reverse_index": str(rev),
                "total": str(tot),
                "delivery": delivery,
                "message_id": interaction.message.id,
                "delivered_message_id": delivered_id,
                "user_id": getattr(getattr(interaction, "user", None), "id", None),
            })
            return
        #Else pull a random recent photo
        pick2 = await get_random_photo(self.cat_name)
        if isinstance(pick2, str):
            delivery, delivered_id = await self._edit_or_send(
                interaction,
                content=pick2,
                embed=None,
                image_bytes=None,
            )
            log_event({
                "event": "show_photo_page",
                "cat": self.cat_name,
                "source": "error",
                "delivery": delivery,
                "message_id": interaction.message.id,
                "delivered_message_id": delivered_id,
                "user_id": getattr(getattr(interaction, "user", None), "id", None),
                "detail": pick2,
            })
            return
        full = self.cat_name
        display = _single_display_name(full, self.cat_name)
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
            delivery, delivered_id = await self._edit_or_send(
                interaction,
                embed=e2,
                image_bytes=img_bytes_for_embed,
                filename="crop.jpg",
            )
        else:
            if img_url:
                e2.set_image(url=img_url)
            delivery, delivered_id = await self._edit_or_send(
                interaction,
                embed=e2,
                image_bytes=None,
            )

        log_event({
            "event": "show_photo_page",
            "cat": self.cat_name,
            "source": "recent",
            "serial": str(pick2.get('serial','Unknown')),
            "reverse_index": str(pick2.get('reverse_index','?')),
            "total": str(pick2.get('total_available','?')),
            "delivery": delivery,
            "message_id": interaction.message.id,
            "delivered_message_id": delivered_id,
            "user_id": getattr(getattr(interaction, "user", None), "id", None),
            "cropped": bool(img_bytes_for_embed),
        })


def _add_field(embed: discord.Embed, name: str, value: Any, inline: bool = True) -> None:
    """Helper that conditionally adds trimmed embed fields."""
    if value is None:
        return
    s = str(value).strip()
    if not s:
        return
    embed.add_field(name=name, value=s[:1024], inline=inline)

async def handle_cat_show(intent: 'Intent', ctx: dict) -> None:
    """Serve a random photo and profile snapshot for the requested cat."""
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Which cat would you like to see? Ex: `TomCat, show Microwave`")
        return

    #Try cached photo first for speed without hitting Sheets
    cached_bytes: Optional[bytes] = None
    cached_meta = None
    cached_bytes, cached_meta = await pop_one_cached(name, use_sheet=False)
    if cached_bytes:
        #Send immediate embed; enrich from cached sidecar profile if available (no live sheet)
        display = _single_display_name(cached_meta.get('display_name') or name, name) if isinstance(cached_meta, dict) else _single_display_name(name, name)
        title = f"__**Random Photo of {display}**__"
        desc = f"**Here's a random photo of {display}**\n(Photo {cached_meta.get('reverse_index','?')} out of {cached_meta.get('total_available','?')})\nImage: {cached_meta.get('serial','Unknown')}" if isinstance(cached_meta, dict) else None
        embed = discord.Embed(title=title, description=desc or "", color=0x2F3136)
        #No extra profile fields in the caption
        file = discord.File(io.BytesIO(cached_bytes), filename="cache.jpg")
        embed.set_image(url="attachment://cache.jpg")
        await ch.send(embed=embed, file=file)

        #Refill cache in background (exclude served serial)
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

    #Fallback: fetch a random recent photo and send simple caption
    pick = await get_random_photo(name)
    if isinstance(pick, str):
        #Try resolving via profile, then retry once with actual name
        profile = await get_cat_profile(name)
        if isinstance(profile, dict):
            pick = await get_random_photo(profile.get('actual_name') or name)
        if isinstance(pick, str):
            await ch.send(pick)
            return
    display = _single_display_name(pick.get('actual_name') or name, name)
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
    """Fetch an image URL to a temp file so crops can run locally."""
    os.makedirs(dest_dir, exist_ok=True)
    parsed = urlparse(url)
    host = parsed.hostname
    if host:
        if host.lower() == "localhost" or host.lower().endswith(".local"):
            raise ValueError("Blocked local address")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
            raise ValueError("Blocked local address")
    fname = url.split("?")[0].split("/")[-1] or "photo.jpg"
    fname = os.path.basename(fname)
    fname = "".join(c for c in fname if c.isalnum() or c in {".", "_", "-"}) or "photo.jpg"
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
    """Send a single photo embed with manual paging buttons."""
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Which cat would you like to see? Ex: `TomCat, show me Microwave`")
        return

    #Try cache first without hitting Sheets
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
        display = _single_display_name(cached_meta2.get('display_name') or name, name) if isinstance(cached_meta2, dict) else _single_display_name(name, name)
    else:
        #Fallback to sheet-backed path
        profile = await get_cat_profile(name)
        if isinstance(profile, str):
            await ch.send(profile)
            return
        actual = profile["actual_name"]
        display = _single_display_name(actual, name)
        pick = await get_random_photo(actual)
        if isinstance(pick, str):
            await ch.send(pick)
            return
        img_url = pick.get("url")
        img_bytes_for_embed: Optional[bytes] = None
        tmp: Optional[str] = None

    #Try fast auto-crop if enabled
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
        #Pass FULL_NAME: use cached_meta's full_name when cached, else actual
        full_for_button = (cached_meta2.get('full_name') if isinstance(cached_meta2, dict) else None) or (actual if not cached_bytes2 else name)
        await ch.send(embed=embed, file=file, view=PhotoView(full_for_button))
    else:
        if img_url:
            embed.set_image(url=img_url)
        await ch.send(embed=embed, view=PhotoView(actual))



#Optional: tiny wrapper to expose a strict "who is" alias if you want a separate name
async def handle_cat_profile(intent: 'Intent', ctx: dict) -> None:
    """Render a cat profile card sourced from cached Sheets data."""
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Which cat would you like to see? Ex: `TomCat, who is Microwave`")
        return
    #Prefer cached profile snapshot to avoid live sheet; fall back to sheet builder
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
    #Build embed from cached profile with classic text layout
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
    age_raw = prof.get("birthday_estimate") or prof.get("age")
    age_formatted = _format_age_value(age_raw)
    if age_formatted:
        lines.append(f"**Age Estimate:** {age_formatted}")
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
    #Image: prefer most-recent cached JPEG to avoid Sheets; else profile image_url; else skip
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
