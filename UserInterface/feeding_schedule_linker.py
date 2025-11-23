"""Simple responder to post the TomCat feeding schedule link/button."""

from __future__ import annotations

from typing import Any, Dict
import discord

from tomcat.logger import log_action


async def handle_feeding_schedule_link(ctx: Dict[str, Any]) -> None:
    channel = ctx["channel"]
    embed = discord.Embed(
        title="Feeding schedule",
        description="Click the link below to open the feeding schedule on TomCat-IV UI.",
        color=0x5865F2,
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open feeding schedule", url="https://pytomcat-ui.local/"))  # replace with real URL if different
    try:
        sent = await channel.send(embed=embed, view=view)
    except Exception as exc:
        log_action("feeding_schedule_link_error", f"channel={getattr(channel, 'id', None)}", str(exc))
        return
    log_action(
        "feeding_schedule_link",
        f"user={getattr(ctx.get('author'), 'id', 0)}; channel={getattr(channel, 'id', None)}",
        f"message_id={getattr(sent, 'id', None)}",
    )
