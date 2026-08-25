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
from typing import Optional
from ..services import profile_cache as PC, local_photos
from ..config import settings
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
    #Every photo attachment in this module funnels through here, so bounding the
    #size once covers all of them. Oversized bytes previously reached ch.send()
    #and raised 413 (code 40005), which escaped as intent_router_error and left
    #the user's request silently unanswered.
    limit = int(getattr(settings, "discord_attachment_max_bytes", 8 * 1024 * 1024) or 0)
    if limit > 0 and len(data) > limit:
        original = len(data)
        data, suffix = await asyncio.to_thread(
            local_photos.fit_attachment_bytes, data, suffix, limit
        )
        log_action(
            "photo_attachment_downscaled",
            f"serial={sn}",
            f"{original} -> {len(data)} bytes (limit {limit})",
        )
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


async def _build_local_show_image_payload(serial: Any) -> tuple[Optional[bytes], str, bool]:
    """Return original local photo bytes for a show-photo response."""
    raw_bytes, suffix = await _read_local_photo_bytes(serial)
    filename = _attachment_filename_for_serial(serial, suffix)
    if not raw_bytes:
        return None, filename, False
    return raw_bytes, filename, False


async def _resolve_random_photo_pick(name: str) -> dict | str:
    """Resolve a random local photo using metadata rows, retrying via actual name when needed."""
    pick = await get_random_photo(name)
    if not isinstance(pick, str):
        return pick
    profile = await get_cat_profile(name)
    if isinstance(profile, dict):
        actual = str(profile.get("actual_name") or name).strip()
        if actual:
            retry = await get_random_photo(actual)
            if not isinstance(retry, str):
                return retry
    return pick


async def _build_latest_profile_image_payload(actual_name: str) -> tuple[Optional[bytes], Optional[str]]:
    """Resolve the best available local image payload for a profile response."""
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

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        """Log callback failures so button issues are diagnosable from machine logs."""
        item_id = getattr(item, "custom_id", None) or getattr(item, "label", "?")
        log_action("photo_view_callback_error", str(self.cat_name), f"item={item_id}; {type(error).__name__}: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Couldn't load another photo right now.", ephemeral=True)
            else:
                await interaction.response.send_message("Couldn't load another photo right now.", ephemeral=True)
        except Exception:
            pass

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
            await interaction.message.edit(
                content=content,
                embed=embed,
                attachments=attachments,
                view=self,
            )
            return "edit", interaction.message.id
        except NotFound:
            pass  #Fall through to followup.send
        except Exception as e:
            log_action("photo_view_edit_error", str(self.cat_name), f"{type(e).__name__}: {e}")

        #Try followup.send as fallback when the original message is gone.
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

    @discord.ui.button(
        label="Show me another",
        style=discord.ButtonStyle.primary,
        custom_id="show_photo_another",
    )
    async def another(self, interaction: discord.Interaction, button: discord.ui.Button):
        #Defer immediately to avoid 3s interaction timeout
        log_action(
            "show_photo_button_click",
            str(self.cat_name),
            f"message_id={getattr(getattr(interaction, 'message', None), 'id', None)}; user_id={getattr(getattr(interaction, 'user', None), 'id', None)}",
        )
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass
        pick2 = await _resolve_random_photo_pick(self.cat_name)
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
            "matched_label": pick2.get("matched_label"),
            "matched_box_index": pick2.get("matched_box_index"),
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

    pick = await _resolve_random_photo_pick(name)
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

    profile = await get_cat_profile(name)
    if isinstance(profile, str):
        await ch.send(profile)
        return
    full_for_button = str(profile.get("actual_name") or name).strip() or name
    display = _single_display_name(full_for_button, name)
    pick = await _resolve_random_photo_pick(full_for_button)
    if isinstance(pick, str):
        await ch.send(pick)
        return

    title = f"__**Random Photo of {display}**__"
    desc = (
        f"**Here's a random photo of {display}**\n"
        f"(Photo {pick.get('reverse_index','?')} out of {pick.get('total_available','?')})\n"
        f"Image: {pick.get('serial','Unknown')}"
    )
    embed = discord.Embed(title=title, description=desc, color=0x2F3136)
    img_bytes_for_embed, filename, cropped_initial = await _build_local_show_image_payload(pick.get("serial"))
    if img_bytes_for_embed:
        file = discord.File(io.BytesIO(img_bytes_for_embed), filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        sent = await ch.send(embed=embed, file=file, view=PhotoView(full_for_button))
        log_event({
            "event": "show_photo_page",
            "cat": full_for_button,
            "source": "initial",
            "serial": str(pick.get('serial','Unknown')),
            "reverse_index": str(pick.get('reverse_index','?')),
            "total": str(pick.get('total_available','?')),
            "matched_label": pick.get("matched_label"),
            "matched_box_index": pick.get("matched_box_index"),
            "delivery": "send",
            "message_id": getattr(sent, "id", None),
            "delivered_message_id": getattr(sent, "id", None),
            "user_id": None,
            "cropped": bool(cropped_initial),
        })
        return
    log_action("show_photo_local_missing", str(full_for_button), f"serial={pick.get('serial','Unknown')}")
    sent = await ch.send(embed=embed, view=PhotoView(full_for_button))
    log_event({
        "event": "show_photo_page",
        "cat": full_for_button,
        "source": "initial",
        "serial": str(pick.get('serial','Unknown')),
        "reverse_index": str(pick.get('reverse_index','?')),
        "total": str(pick.get('total_available','?')),
        "matched_label": pick.get("matched_label"),
        "matched_box_index": pick.get("matched_box_index"),
        "delivery": "send",
        "message_id": getattr(sent, "id", None),
        "delivered_message_id": getattr(sent, "id", None),
        "user_id": None,
        "cropped": bool(cropped_initial),
    })


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
        #Sourced from CatDatabase "Last Seen By", which sync_catabase_photo_columns
        #sets to the author of the most recent photo. Labelling that "Reported"
        #collided with the actual report flow and read as though posting a photo
        #had filed a report on the poster's behalf.
        lines.append("**Last Seen:** " + " ".join(last_bits))
    nicks = prof.get("nicknames")
    if nicks:
        lines.append(f"**Common Nicknames:** {nicks}")
    comm = prof.get("comments")
    if comm:
        lines.append(f"**Comments:** {comm}")
    e.description = "\n".join(lines)
    img_bytes, filename = await _build_latest_profile_image_payload(str(actual))
    if img_bytes and filename:
        try:
            file = discord.File(io.BytesIO(img_bytes), filename=filename)
            e.set_image(url=f"attachment://{filename}")
            await ch.send(embed=e, file=file)
            return
        except Exception as exc:
            #A photo that can't be uploaded (too large, unsupported, network error)
            #must not swallow the whole profile response. Fall back to text-only so
            #"who is <cat>" always replies. This notably affects heavily photographed
            #cats like Microwave, whose latest image can exceed Discord's upload limit.
            log_action("cat_profile_image_send_failed", str(actual), f"{type(exc).__name__}: {exc}")
            e.set_image(url=None)
    await ch.send(embed=e)
