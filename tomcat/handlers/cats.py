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
)
from ..logger import log_action, log_event 
import re
import io, asyncio
from datetime import datetime, timezone
from ..vision import vision as V
from ..services.show_cache import pop_one_cached, ensure_cat_cache, latest_cached_bytes
from typing import Optional
from ..config import settings
from ..services import profile_cache as PC, local_photos
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


def _parse_serial_value(value: Any) -> Optional[int]:
    """Parse any serial-like token into an integer."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    try:
        parsed = int(digits)
        return parsed if parsed > 0 else None
    except Exception:
        return None


async def _read_local_photo_bytes(serial: Any) -> tuple[Optional[bytes], str]:
    """Load the original local photo bytes for a serial."""
    sn = _parse_serial_value(serial)
    if sn is None:
        return None, ".jpg"
    path = local_photos.get_local_photo_path(sn)
    suffix = str(getattr(path, "suffix", "") or ".jpg").lower() if path is not None else ".jpg"
    if path is None or not path.is_file():
        return None, suffix
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except Exception:
        return None, suffix
    return data, suffix


def _attachment_filename_for_serial(serial: Any, suffix: str = ".jpg", *, cropped: bool = False) -> str:
    """Build a stable attachment filename for locally served photos."""
    sn = _parse_serial_value(serial)
    base = f"sn{int(sn):04d}" if sn is not None else "photo"
    if cropped:
        return f"{base}_crop.jpg"
    ext = str(suffix or ".jpg").strip().lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    return f"{base}{ext}"


def _crop_local_photo(raw_bytes: bytes) -> Optional[bytes]:
    """Run the existing CV cropper against already-local bytes."""
    crops = V.crop(raw_bytes)
    return crops[0] if len(crops) == 1 else None


async def _build_local_show_image_payload(serial: Any) -> tuple[Optional[bytes], str, bool]:
    """Return bytes/filename for a local show-photo response, preferring an auto-crop."""
    raw_bytes, suffix = await _read_local_photo_bytes(serial)
    filename = _attachment_filename_for_serial(serial, suffix)
    if not raw_bytes:
        return None, filename, False
    if settings.auto_crop_show_photo:
        try:
            cropped = await asyncio.wait_for(
                asyncio.to_thread(_crop_local_photo, raw_bytes),
                timeout=(settings.cv_timeout_ms / 1000.0),
            )
            if cropped:
                return cropped, _attachment_filename_for_serial(serial, ".jpg", cropped=True), True
        except Exception:
            pass
    return raw_bytes, filename, False


async def _build_latest_profile_image_payload(actual_name: str) -> tuple[Optional[bytes], Optional[str]]:
    """Resolve the best available local image payload for a profile response."""
    cached = latest_cached_bytes(actual_name) or None
    if cached:
        return cached, "recent.jpg"
    recent = await get_latest_photo(actual_name)
    if not isinstance(recent, dict):
        return None, None
    raw_bytes, suffix = await _read_local_photo_bytes(recent.get("serial"))
    if not raw_bytes:
        return None, None
    return raw_bytes, _attachment_filename_for_serial(recent.get("serial"), suffix)


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
        If the webhook is fully expired, returns ("expired", None).
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
            pass  #Fall through to followup.send

        #Try followup.send as fallback
        try:
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
        except NotFound:
            #Webhook fully expired (>15 min since original interaction)
            log_action("photo_view_expired", "webhook", f"cat={self.cat_name}")
            return "expired", None

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
        image_bytes, filename, cropped = await _build_local_show_image_payload(pick2.get("serial"))
        if image_bytes:
            e2.set_image(url=f"attachment://{filename}")
            delivery, delivered_id = await self._edit_or_send(
                interaction,
                embed=e2,
                image_bytes=image_bytes,
                filename=filename,
            )
        else:
            log_action("show_photo_local_missing", str(self.cat_name), f"serial={pick2.get('serial','Unknown')}")
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
            "cropped": bool(cropped),
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
    image_bytes, filename, _ = await _build_local_show_image_payload(pick.get("serial"))
    if image_bytes:
        file = discord.File(io.BytesIO(image_bytes), filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        await ch.send(embed=embed, file=file)
        return
    log_action("show_photo_local_missing", str(name), f"serial={pick.get('serial','Unknown')}")
    await ch.send(embed=embed)


async def handle_cat_photo(intent: 'Intent', ctx: dict) -> None:
    """Send a single photo embed with manual paging buttons."""
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Which cat would you like to see? Ex: `TomCat, show me Microwave`")
        return

    #Try cache first without hitting Sheets
    cached_bytes2, cached_meta2 = await pop_one_cached(name, use_sheet=False)
    full_for_button = name
    if cached_bytes2:
        ex2 = set()
        try:
            if isinstance(cached_meta2, dict) and cached_meta2.get('serial'):
                import re as _re
                ex2.add(_re.sub(r"[^0-9]", "", str(cached_meta2['serial'])))
        except Exception:
            pass
        full_for_button = (cached_meta2.get('full_name') if isinstance(cached_meta2, dict) else None) or name
        asyncio.create_task(ensure_cat_cache(full_for_button, settings.show_cache_per_cat, exclude_serials=ex2))
        img_bytes_for_embed: Optional[bytes] = cached_bytes2
        total_avail = cached_meta2.get('total_available','?') if isinstance(cached_meta2, dict) else '?'
        reverse_idx = cached_meta2.get('reverse_index','?') if isinstance(cached_meta2, dict) else '?'
        serial = cached_meta2.get('serial','cached') if isinstance(cached_meta2, dict) else 'cached'
        display = _single_display_name(cached_meta2.get('display_name') or name, name) if isinstance(cached_meta2, dict) else _single_display_name(name, name)
    else:
        # Fallback to CatDatabase-backed lookup for the requested cat name.
        profile = await get_cat_profile(name)
        if isinstance(profile, str):
            await ch.send(profile)
            return
        actual = profile["actual_name"]
        display = _single_display_name(actual, name)
        full_for_button = actual
        pick = await get_random_photo(actual)
        if isinstance(pick, str):
            await ch.send(pick)
            return
        img_bytes_for_embed = None

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
    filename = "cache.jpg"

    if img_bytes_for_embed:
        file = discord.File(io.BytesIO(img_bytes_for_embed), filename="cache.jpg")
        embed.set_image(url="attachment://cache.jpg")
        await ch.send(embed=embed, file=file, view=PhotoView(full_for_button))
        return
    if not cached_bytes2:
        img_bytes_for_embed, filename, _ = await _build_local_show_image_payload(pick.get("serial"))
    if img_bytes_for_embed:
        file = discord.File(io.BytesIO(img_bytes_for_embed), filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        await ch.send(embed=embed, file=file, view=PhotoView(full_for_button))
        return
    if not cached_bytes2:
        log_action("show_photo_local_missing", str(full_for_button), f"serial={pick.get('serial','Unknown')}")
    await ch.send(embed=embed, view=PhotoView(full_for_button))



#Optional: tiny wrapper to expose a strict "who is" alias if you want a separate name
async def handle_cat_profile(intent: 'Intent', ctx: dict) -> None:
    """Render a cat profile card sourced from cached Sheets data."""
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Which cat would you like to see? Ex: `TomCat, who is Microwave`")
        return
    prof = PC.get_profile(name)
    if not prof:
        live_profile = await get_cat_profile(name)
        if isinstance(live_profile, str):
            await ch.send(live_profile)
            return
        prof = live_profile
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
    img_bytes, filename = await _build_latest_profile_image_payload(str(actual))
    if img_bytes and filename:
        file = discord.File(io.BytesIO(img_bytes), filename=filename)
        e.set_image(url=f"attachment://{filename}")
        await ch.send(embed=e, file=file)
        return
    await ch.send(embed=e)
