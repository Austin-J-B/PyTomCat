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


class UIActivityView(discord.ui.View):
    """Persistent button that spawns a Discord Activity invite on demand."""

    def __init__(self, *, app_id: Optional[int], guild: Optional[discord.Guild]):
        super().__init__(timeout=None)
        self._app_id = app_id
        self._guild = guild

    def _resolve_channel(self, interaction: discord.Interaction) -> Optional[discord.abc.GuildChannel]:
        """Pick the voice/stage channel to host the Activity."""
        guild = interaction.guild or self._guild
        if guild:
            mapped_id = DEFAULT_ACTIVITY_CHANNELS.get(int(guild.id))
            if mapped_id:
                mapped = guild.get_channel(mapped_id)
                if isinstance(mapped, (discord.VoiceChannel, discord.StageChannel)):
                    return mapped

        member = getattr(interaction, "user", None)
        voice_state = getattr(member, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel

        return None

    @discord.ui.button(label="Test", style=discord.ButtonStyle.primary, custom_id="tomcat_ui_launch")
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        app_id = self._app_id
        if not app_id:
            await interaction.response.send_message(
                "TomCat UI isn't configured yet. Ask an admin to set `UITEST_ACTIVITY_APP_ID`.",
                ephemeral=True,
            )
            return

        channel = self._resolve_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "Join a voice or stage channel, then press **Test** again.",
                ephemeral=True,
            )
            return

        if not getattr(channel, "permissions_for", None):
            await interaction.response.send_message(
                "I couldn't determine permissions for the target channel.",
                ephemeral=True,
            )
            return

        guild_me = getattr(channel.guild, "me", None) if getattr(channel, "guild", None) else None
        if guild_me and not channel.permissions_for(guild_me).create_instant_invite:
            await interaction.response.send_message(
                "I need the **Create Invite** permission in that voice channel to launch the UI.",
                ephemeral=True,
            )
            return

        try:
            invite = await channel.create_invite(
                max_age=300,
                max_uses=0,
                target_application_id=int(app_id),
                target_type=discord.InviteTarget.embedded_application,
                reason="TomCat UI activity launch",
            )
        except Exception as exc:
            log_action(
                "ui_activity_invite_error",
                f"user={getattr(interaction.user, 'id', 0)}; channel={getattr(channel, 'id', 0)}",
                str(exc),
            )
            await interaction.response.send_message(
                "Something went wrong while creating the Activity invite. Check logs for details.",
                ephemeral=True,
            )
            return

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open TomCat UI", url=str(invite.url)))
        await interaction.response.send_message(
            "Click below to open the TomCat UI.",
            view=view,
            ephemeral=True,
        )

        log_action(
            "ui_activity_launched",
            f"user={getattr(interaction.user, 'id', 0)}; channel={getattr(channel, 'id', 0)}",
            f"invite={getattr(invite, 'code', '?')}",
        )


async def handle_ui_launch(ctx: Dict[str, Any]) -> None:
    """Publish the UI launch button in the current channel."""
    channel = ctx["channel"]
    message = ctx.get("message")
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)

    app_id = _get_activity_app_id()
    embed = discord.Embed(
        title="TomCat-UI",
        description="Press **Test** any time to open the TomCat interface.",
        color=0x5865F2,
    )

    view = UIActivityView(app_id=app_id, guild=guild)

    try:
        sent = await channel.send(embed=embed, view=view)
    except Exception as exc:
        log_action("ui_activity_message_error", f"channel={getattr(channel, 'id', None)}", str(exc))
        return

    log_action(
        "ui_activity_prompt",
        f"user={getattr(ctx.get('author'), 'id', 0)}; channel={getattr(channel, 'id', None)}",
        f"message_id={getattr(sent, 'id', None)}; app_id={app_id or 'unset'}",
    )
