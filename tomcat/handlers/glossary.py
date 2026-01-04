"""Function glossary command: paginated embed listing available TomCat commands."""
from __future__ import annotations

import discord
from typing import List, Tuple

from ..config import settings

#Command definitions: (display_format, description)
USER_COMMANDS: List[Tuple[str, str]] = [
    ("TomCat, show me [cat name]", "Shows a photo of the specified cat."),
    ("TomCat, who is [cat name]", "Shows the profile card for a cat."),
    ("TomCat, who is this?", "Identifies the cat in an attached or recent image."),
    ("TomCat, identify", "Runs cat identification on an attached image."),
    ("TomCat, who lives at [station]?", "Lists which cats live at a feeding station."),
    ("TomCat, feeding update", "Shows today's feeding status for all stations."),
    ("TomCat, who feeds today?", "Shows who is scheduled to feed today."),
    ("TomCat, who is feeding [day]?", "Shows feeder schedule for a specific day."),
    ("TomCat, feeding schedule", "Returns the link to the feeding schedule."),
    ("TomCat, find a sub", "Returns the link to request a feeding substitute."),
    ("TomCat, functions", "Shows this command list."),
]

ADMIN_COMMANDS: List[Tuple[str, str]] = [
    ("TomCat, check emails", "Scans the last 99 emails for finance/dues entries."),
    ("TomCat, log the past [N] emails", "Logs and processes the last N emails."),
    ("TomCat, check the last email", "Shows the most recent email received."),
    ("TomCat, log last [N] finances", "Processes the last N financial emails."),
    ("TomCat, check due payments", "Runs a dues check on the portal channel."),
    ("TomCat, update due-paying members", "Full dues update: check, verify, sync roles."),
    ("TomCat, run dues perks", "Grants perks to verified dues-paying members."),
    ("TomCat, run dues job", "Manually triggers the daily dues scheduler."),
    ("TomCat, recache [cat name]", "Refreshes the photo cache for a specific cat."),
    ("TomCat, recache all photos", "Refreshes the entire cat photo cache."),
    ("TomCat, recache catabase", "Reloads cat names from the catabase sheet."),
    ("TomCat, silent mode on/off", "Toggles quiet mode for bot responses."),
    ("TomCat, manual 8pm update", "Previews the 8pm feeding reminder."),
    ("TomCat, create profile [N]", "Creates profile card for cat number N."),
    ("TomCat, update all profiles", "Regenerates all cat profile cards."),
    ("TomCat, remove role [ID]", "Removes a role from all server members."),
]

MAX_LINES_PER_PAGE = 30

def _format_entry(cmd: str, desc: str) -> str:
    """Format a single command entry as bold command + description."""
    return f"**{cmd}**\n{desc}"

def _count_lines(text: str) -> int:
    """Count display lines in formatted text."""
    return text.count('\n') + 1

def _build_pages(commands: List[Tuple[str, str]], header: str = "") -> List[str]:
    """Build pages from command list, respecting line limits."""
    pages: List[str] = []
    current_lines: List[str] = []
    current_line_count = 0
    
    if header:
        current_lines.append(header)
        current_line_count = _count_lines(header) + 1  #+1 for blank after header
    
    for cmd, desc in commands:
        entry = _format_entry(cmd, desc)
        entry_lines = _count_lines(entry)
        
        #If adding this entry would exceed limit, start new page
        if current_line_count + entry_lines > MAX_LINES_PER_PAGE and current_lines:
            pages.append("\n".join(current_lines))
            current_lines = []
            current_line_count = 0
            if header:
                current_lines.append(header)
                current_line_count = _count_lines(header) + 1
        
        current_lines.append(entry)
        current_line_count += entry_lines + 1  #+1 for spacing between entries
    
    if current_lines:
        pages.append("\n".join(current_lines))
    
    return pages

def _build_glossary_pages(is_admin: bool) -> List[str]:
    """Build all pages for the glossary based on user permissions."""
    all_pages: List[str] = []
    
    #User commands section
    user_header = "__**User Commands**__\n*All functions below require the 'TomCat,' prefix.*\n"
    user_pages = _build_pages(USER_COMMANDS, user_header)
    all_pages.extend(user_pages)
    
    #Admin commands section (only for admins/officers)
    if is_admin:
        admin_header = "__**Admin Commands**__\n*All functions below require the 'TomCat,' prefix.*\n"
        admin_pages = _build_pages(ADMIN_COMMANDS, admin_header)
        all_pages.extend(admin_pages)
    
    return all_pages


class GlossaryView(discord.ui.View):
    """Paginated view for the function glossary."""
    
    def __init__(self, pages: List[str], author_id: int):
        super().__init__(timeout=300)  #5 minute timeout
        self.pages = pages
        self.current_page = 0
        self.author_id = author_id
        self._update_buttons()
    
    def _update_buttons(self):
        """Show/hide buttons based on current page."""
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.pages) - 1
        
        #Hide buttons entirely if only one page
        if len(self.pages) <= 1:
            self.prev_button.style = discord.ButtonStyle.secondary
            self.next_button.style = discord.ButtonStyle.secondary
            self.prev_button.disabled = True
            self.next_button.disabled = True
    
    def _build_embed(self) -> discord.Embed:
        """Build the embed for the current page."""
        embed = discord.Embed(
            title="TomCat Function List\n",
            description=self.pages[self.current_page],
            color=0x5865F2  #Discord blurple
        )
        if len(self.pages) > 1:
            embed.set_footer(text=f"Page {self.current_page + 1} of {len(self.pages)}")
        return embed
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow the original requester to use buttons."""
        if interaction.user and interaction.user.id == self.author_id:
            return True
        try:
            await interaction.response.send_message(
                "Only the person who requested this can use these buttons.",
                ephemeral=True
            )
        except Exception:
            pass
        return False
    
    @discord.ui.button(label="Prev Page", style=discord.ButtonStyle.primary, custom_id="glossary_prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
    
    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.primary, custom_id="glossary_next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)


async def handle_function_glossary(intent, ctx, is_admin: bool) -> None:
    """Handle the function glossary command."""
    channel = ctx.get("channel")
    author = ctx.get("author")
    if not channel:
        return
    
    author_id = int(getattr(author, "id", 0) or 0)
    pages = _build_glossary_pages(is_admin)
    
    if not pages:
        pages = ["No commands available."]
    
    view = GlossaryView(pages, author_id)
    embed = view._build_embed()
    
    try:
        await channel.send(embed=embed, view=view)
    except Exception:
        #Fallback to plain text if embed fails
        try:
            await channel.send(pages[0][:2000])
        except Exception:
            pass
