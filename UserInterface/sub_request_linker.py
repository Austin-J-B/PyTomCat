"""Responder that posts a link/button to the sub-request UI page."""

from __future__ import annotations

from typing import Any, Dict
import discord

from tomcat.logger import log_action


SUB_REQUEST_URL = "https://ui.catsofuta.org/#sub"
OPEN_SUB_REQUESTS_URL = "https://ui.catsofuta.org/#claim"


def build_open_sub_requests_view() -> discord.ui.View:
    """Reusable view that links directly to the open sub requests page."""
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open sub requests", url=OPEN_SUB_REQUESTS_URL))
    return view


async def handle_sub_request_link(ctx: Dict[str, Any]) -> None:
    channel = ctx["channel"]
    embed = discord.Embed(
        title="Sub request",
        description="Click the link below to open the sub request form on TomCat-IV UI.",
        color=0x5865F2,
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open sub request", url=SUB_REQUEST_URL))
    try:
        sent = await channel.send(embed=embed, view=view)
    except Exception as exc:
        log_action("sub_request_link_error", f"channel={getattr(channel, 'id', None)}", str(exc))
        return
    log_action(
        "sub_request_link",
        f"user={getattr(ctx.get('author'), 'id', 0)}; channel={getattr(channel, 'id', None)}",
        f"message_id={getattr(sent, 'id', None)}",
    )
