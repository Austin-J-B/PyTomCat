"""Cats feature: show cat profiles and photos."""
from __future__ import annotations
import discord
from typing import Any
from tomcat.services.catsheets import get_cat_profile, get_random_photo
from tomcat.utils.sender import safe_send
from tomcat.logger import log_action

class CatPhotoView(discord.ui.View):
    def __init__(self, cat_query: str):
        super().__init__(timeout=180)
        self.cat_query = cat_query

    @discord.ui.button(label="Show me another", style=discord.ButtonStyle.primary)
    async def show_another(self, interaction: discord.Interaction, button: discord.ui.Button):
        rp = await get_random_photo(self.cat_query)
        if isinstance(rp, str):
            await interaction.response.edit_message(content=rp, embed=None, view=None)
            return
        await interaction.response.edit_message(embed=build_random_photo_embed(rp), view=self)


def build_profile_embed(info: dict) -> discord.Embed:
    title = f"__**{info['actual_name']}**__"
    lines = [
        f"**Description:** {info.get('physical_description','Unknown')}",
        f"**Behavior:** {info.get('behavior','Unknown')}",
        f"**Location:** {info.get('location','Unknown')}",
        f"**Age Estimate:** {info.get('age_estimate','Unknown')}",
        f"**TNR Status:** {info.get('tnr_status','Unknown')}",
    ]
    if info.get('nicknames'):
        lines.append(f"**Common Nicknames:** {info['nicknames']}")
    lines.append(
        f"**Last Reported:** {info.get('last_seen_date','Unknown')} at {info.get('last_seen_time','Unknown')} by {info.get('last_seen_by','Unknown')}"
    )
    emb = discord.Embed(title=title, description="\n".join(lines), color=0x2F3136)
    if info.get('image_url'):
        emb.set_image(url=info['image_url'])
    return emb


def build_random_photo_embed(rp: dict) -> discord.Embed:
    emb = discord.Embed(
        title=f"__**Random Photo of {rp['actual_name']}**__",
        description=(
            f"**Here's a random photo of {rp['actual_name']}**\n"
            f"(Photo {rp['reverse_index']} out of {rp['total_available']})\n"
            f"Image: {rp['serial']}"
        ),
        color=0x2F3136,
    )
    emb.set_image(url=rp['url'])
    return emb


async def handle_cat_show(intent, ctx: dict[str, Any]):
    name = (intent.args.get("name", "") or "").strip()
    channel: discord.abc.Messageable = ctx["channel"]
    if not name:
        await safe_send(channel, "Which cat would you like to see?")
        log_action("handle_cat_show", "missing name", "prompted")
        return
    info = await get_cat_profile(name)
    if isinstance(info, str):
        await safe_send(channel, info)
        log_action("handle_cat_show", f"name={name}", "not found")
        return
    await safe_send(channel, embed=build_profile_embed(info), view=CatPhotoView(cat_query=name))
    log_action("handle_cat_show", f"name={name}", "sent profile")
