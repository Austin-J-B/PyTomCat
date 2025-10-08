"""Helpers for launching Discord Activities that power the TomCat UI."""

from __future__ import annotations

from typing import Any, Dict, Optional

import discord

from tomcat.config import settings
from tomcat.logger import log_action

# Guild-specific default voice/stage channels for the UI Activity.
DEFAULT_ACTIVITY_CHANNELS: dict[int, int] = {
    551082419768393729: 1425331929422630992,
    798371894985752587: 798371895434149940,
}


def _get_activity_app_id() -> Optional[int]:
    """Read the configured Activity application id with backward compatibility."""
    app_id = getattr(settings, "ui_activity_app_id", None)
    if not app_id:
        app_id = getattr(settings, "uitest_activity_app_id", None)
    return app_id


def _resolve_target_channel(ctx: Dict[str, Any]) -> Optional[discord.abc.GuildChannel]:
    """Determine which voice/stage channel should host the Activity."""
    channel = ctx["channel"]
    message = ctx.get("message")
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
    author = ctx.get("author")
    if guild:
        mapped_id = DEFAULT_ACTIVITY_CHANNELS.get(int(guild.id))
        if mapped_id:
            mapped = guild.get_channel(mapped_id)
            if isinstance(mapped, (discord.VoiceChannel, discord.StageChannel)):
                return mapped

    if author:
        voice_state = getattr(author, "voice", None)
        voice_channel = getattr(voice_state, "channel", None)
        if isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return voice_channel

    return None


async def handle_ui_launch(ctx: Dict[str, Any]) -> None:
    """Publish the UI launch button in the current channel."""
    channel = ctx["channel"]
    message = ctx.get("message")
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)

    app_id = _get_activity_app_id()
    if not app_id:
        await channel.send(
            "TomCat UI isn't configured yet. Ask an admin to set `UITEST_ACTIVITY_APP_ID`.",
        )
        return

    target_channel = _resolve_target_channel(ctx)
    if target_channel is None:
        embed = discord.Embed(
            title="TomCat-UI",
            description="Join any voice or stage channel, then run this command again.",
            color=0x5865F2,
        )
        try:
            sent = await channel.send(embed=embed)
        except Exception as exc:
            log_action("ui_activity_message_error", f"channel={getattr(channel, 'id', None)}", str(exc))
        else:
            log_action(
                "ui_activity_prompt",
                f"user={getattr(ctx.get('author'), 'id', 0)}; channel={getattr(channel, 'id', None)}",
                f"message_id={getattr(sent, 'id', None)}; app_id={app_id}",
            )
        return

    if not getattr(target_channel, "permissions_for", None):
        await channel.send("I couldn't determine permissions for the target channel.")
        return
    guild_me = getattr(target_channel.guild, "me", None) if getattr(target_channel, "guild", None) else None
    if guild_me and not target_channel.permissions_for(guild_me).create_instant_invite:
        await channel.send("I need the **Create Invite** permission in that voice channel to launch the UI.")
        return

    try:
        invite = await target_channel.create_invite(
            max_age=300,
            max_uses=0,
            target_application_id=int(app_id),
            target_type=discord.InviteTarget.embedded_application,
            reason="TomCat UI activity launch",
        )
    except Exception as exc:
        log_action(
            "ui_activity_invite_error",
            f"user={getattr(ctx.get('author'), 'id', 0)}; channel={getattr(target_channel, 'id', 0)}",
            str(exc),
        )
        await channel.send("Something went wrong while creating the Activity invite. Check logs for details.")
        return

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open TomCat UI", url=str(invite.url)))

    embed = discord.Embed(
        title="TomCat-UI",
        description="Click below any time to open the TomCat interface.",
        color=0x5865F2,
    )
    try:
        sent = await channel.send(embed=embed, view=view)
    except Exception as exc:
        log_action("ui_activity_message_error", f"channel={getattr(channel, 'id', None)}", str(exc))
        return

    log_action(
        "ui_activity_prompt",
        f"user={getattr(ctx.get('author'), 'id', 0)}; channel={getattr(channel, 'id', None)}",
        f"message_id={getattr(sent, 'id', None)}; app_id={app_id}; invite={getattr(invite, 'code', '?')}",
    )
