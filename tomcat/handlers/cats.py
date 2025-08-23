from __future__ import annotations
import discord
from typing import Any
from ..intents import Intent
from ..services.catsheets import get_cat_profile, get_recent_photo
from ..logger import log_action  # add at top with the other imports


def _add_field(embed: discord.Embed, name: str, value: Any, inline: bool = True) -> None:
    if value is None:
        return
    s = str(value).strip()
    if not s:
        return
    embed.add_field(name=name, value=s[:1024], inline=inline)

async def handle_cat_show(intent: Intent, ctx: dict) -> None:
    ch: discord.abc.MessageableChannel = ctx["channel"]
    name = intent.data.get("name", "").strip()
    if not name:
        await ch.send("Who am I showing? Try: `TomCat, show Microwave`")
        return

    profile = await get_cat_profile(name)
    if isinstance(profile, str):
        await ch.send(profile)
        return

    # Try to fetch one recent photo for the actual name
    img_url = None
    photo = await get_recent_photo(profile["actual_name"])
    if isinstance(photo, dict):
        img_url = photo.get("url")

    embed = discord.Embed(
        title=profile["actual_name"],
        description=(profile.get("comments") or "").strip(),
        color=0x2f95dc,
    )
    if img_url:
        embed.set_image(url=img_url)

    _add_field(embed, "Physical", profile.get("physical_description"))
    _add_field(embed, "Behavior", profile.get("behavior"))
    _add_field(embed, "Location", profile.get("location"))
    _add_field(embed, "Last Seen Date", profile.get("last_seen_date"))
    _add_field(embed, "Last Seen Time", profile.get("last_seen_time"))
    _add_field(embed, "Last Seen By", profile.get("last_seen_by"))
    _add_field(embed, "Age", profile.get("age"))
    _add_field(embed, "TNR'd", profile.get("tnrd"))
    _add_field(embed, "TNR Date", profile.get("tnr_date"))
    _add_field(embed, "Sex", profile.get("sex"))
    _add_field(embed, "Nicknames", profile.get("nicknames"))

    log_action("handle_cat_show", f"name={profile['actual_name']}", "sending embed")
    await ch.send(embed=embed)
