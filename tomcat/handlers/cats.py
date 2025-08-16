"""Cats feature: "TomCat, show me <cat>"
- Builds an embed with profile info and latest image
- Adds a "Show me another" button that swaps in a random photo
"""
from __future__ import annotations
import discord
from typing import Any
from . .services.catsheets import get_cat_profile, get_random_photo

# ---- UI View with a single button ----
class CatPhotoView(discord.ui.View):
    def __init__(self, cat_query: str):
        super().__init__(timeout=180)
        self.cat_query = cat_query

    @discord.ui.button(label="Show me another", style=discord.ButtonStyle.primary, custom_id="cat_random_more")
    async def show_another(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        rp = await get_random_photo(self.cat_query)
        if isinstance(rp, str):
            await interaction.response.edit_message(content=rp, embeds=[], attachments=[], view=None)
            return
        await interaction.response.edit_message(embed=build_random_photo_embed(rp), view=self)

# ---- Embed builders ----
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
    title = f"__**Random Photo of {rp['actual_name']}**__"
    desc = (
        f"**Here's a random photo of {rp['actual_name']}**\n"
        f"(Photo {rp['reverse_index']} out of {rp['total_available']})\n"
        f"Image: {rp['serial']}"
    )
    emb = discord.Embed(title=title, description=desc, color=0x2F3136)
    emb.set_image(url=rp['url'])
    return emb

# ---- Handler entrypoint ----
async def handle_cat_show(intent, ctx: dict[str, Any]):
    name = (intent.args.get("name", "") or "").strip()
    channel: discord.abc.Messageable = ctx["channel"]
    if not name:
        await channel.send("Which cat would you like to see?")
        return
    info = await get_cat_profile(name)
    if isinstance(info, str):
        await channel.send(info)
        return
    await channel.send(embed=build_profile_embed(info), view=CatPhotoView(cat_query=name))