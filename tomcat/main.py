from __future__ import annotations
import asyncio
import time
from typing import Any, Dict, Union

import discord
from discord.ext import commands
from datetime import datetime, timezone

from .handlers.misc import handle_misc as _handle_misc_raw
from .config import settings
from .logger import log_event, log_action  # noqa: F401  #If unused right now
from .intents import classify, Intent
from .router import Router
from .spam import is_spam



# ------- Discord intents & bot -------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(command_prefix=settings.command_prefix, intents=intents)
router = Router()

# ------- Import real handlers -------
# Cats / Feeding and Dues already match (intent, ctx) in your tree
from .handlers.cats import handle_cat_show as _handle_cat_show
from .handlers.feeding import handle_feeding_status as _handle_feeding_status
from .handlers.dues import (
    handle_dues_notice as _handle_dues_notice,
    process_dues_cycle as _process_dues_cycle,
    init_db as _init_db,
)

# These two do NOT match; they use custom signatures.
from .handlers.admin import handle_silent_mode as _handle_silent_mode_raw
from .handlers.misc import handle_misc as _handle_misc_raw

# --- Muted wrappers: run handlers but drop outbound sends ---
class _MuteChannel:
    def __init__(self, real, label_fn):
        self._real = real
        self._label_fn = label_fn
        self.id = getattr(real, "id", None)
        self.name = getattr(real, "name", None)

    async def send(self, content=None, **kwargs):
        # Log what would have been sent; don’t actually send.
        from .logger import log_action  # local import to avoid cycles
        # Prefer a short preview of content or note an embed
        preview = ""
        if content:
            preview = str(content)
        elif "embed" in kwargs and kwargs["embed"] is not None:
            preview = "embed"
        else:
            preview = "(no content)"
        log_action(
            "muted_send",
            f"channel={self._label_fn(self._real)}",
            preview[:120],
        )
        return None  # mimic coroutine

class _MuteMessage:
    def __init__(self, real_msg, muted_channel):
        # Keep attributes handlers touch; forward everything else if needed
        self._real = real_msg
        self.channel = muted_channel
        self.author = real_msg.author
        self.content = real_msg.content
        self.clean_content = getattr(real_msg, "clean_content", self.content)
        self.attachments = getattr(real_msg, "attachments", [])



async def _handle_misc_adapter(intent: Intent, ctx: Dict[str, Any]) -> None:
    message: discord.Message = ctx["message"]
    await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)


def _user_label(u: Union[discord.Member, discord.User]) -> str:
    return getattr(u, "name", "unknown")



def _channel_label(ch: discord.abc.Messageable) -> str:
    # Guild text channel
    if isinstance(ch, discord.TextChannel):
        return f"#{ch.name}"
    # Thread inside a parent channel; parent can be None so guard it
    if isinstance(ch, discord.Thread):
        parent = getattr(ch, "parent", None)
        parent_prefix = f"#{parent.name}/" if parent and getattr(parent, "name", None) else ""
        return f"{parent_prefix}{ch.name}"
    # 1:1 DM (recipient is Optional[User])
    if isinstance(ch, discord.DMChannel):
        recipient = getattr(ch, "recipient", None)
        if isinstance(recipient, (discord.User, discord.ClientUser)):
            rid = recipient.id
        else:
            rid = "unknown"
        return f"DM:{rid}"
    # Group DM, Stage, Voice, PartialMessageable, whatever else
    name = getattr(ch, "name", None)
    return f"#{name}" if isinstance(name, str) and name else ch.__class__.__name__.lower()



# ------- Adapters to unify handler signatures -------
async def handle_cat_show(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_cat_show(intent, ctx)

async def handle_feeding_status(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_feeding_status(intent, ctx)

async def handle_dues_notice(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_dues_notice(intent, ctx)

# Your admin handler expects (args, ctx) where args == intent.data
async def handle_silent_mode(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_silent_mode_raw(intent.data, ctx)

# Your misc handler expects (message, *, now_ts, allow_in_channels)
async def handle_misc(intent: Intent, ctx: Dict[str, Any]) -> None:
    message: discord.Message = ctx["message"]
    await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)

# ------- Router registration -------
router.register("cat_show", handle_cat_show)
router.register("feeding_status", handle_feeding_status)
router.register("dues_notice", handle_dues_notice)
router.register("silent_mode", handle_silent_mode)
router.register("misc", _handle_misc_adapter)


# ------- Optional: invite cache you already had -------
invites_cache: dict[int, dict[str, int]] = {}
message_cache: dict[int, str] = {}

async def _refresh_invites(guild: discord.Guild):
    """Refreshes the invite cache for a given guild."""
    if not guild.me.guild_permissions.manage_guild:
        print(f"Warning: Missing 'Manage Server' permission in '{guild.name}' to track invites.")
        return
    invites = await guild.invites()
    invites_cache[guild.id] = {inv.code: inv.uses or 0 for inv in invites}

# ------- Lifecycle -------
@bot.event
async def on_ready():
    print(f"[TomCat] Logged in as {bot.user} in {len(bot.guilds)} guild(s).")
    # Machine + human “ONLINE” handled by logger.log_event
    log_event({
        "event": "online",
        "user": str(bot.user),
        "guild_count": len(bot.guilds),
    })

    async def _dues_loop():
        while True:
            try:
                await _process_dues_cycle(bot)
            except Exception as e:
                log_event({"event": "dues_loop_error", "error": str(e)})
            await asyncio.sleep(7200)

    asyncio.create_task(_dues_loop())


# ------- Message entrypoint -------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Human + machine log of the incoming message
    log_event({
        "event": "message",
        "author": _user_label(message.author),
        "channel": _channel_label(message.channel),
        "content": message.clean_content if isinstance(message.content, str) else "",
        "attachments": len(message.attachments) if hasattr(message, "attachments") else 0,
    })

    if is_spam(message.content):
        return

    intent = classify(message.content, channel_id=message.channel.id)
    ctx: Dict[str, Any] = {"bot": bot, "message": message, "channel": message.channel, "author": message.author}

    # Global mute: while silent_mode is ON, only allow the silent_mode toggle intent through.
    if settings.silent_mode and (not intent or intent.type != "silent_mode"):
        muted_ch = _MuteChannel(message.channel, _channel_label)
        muted_msg = _MuteMessage(message, muted_ch)
        ctx["channel"] = muted_ch
        ctx["message"] = muted_msg
        if intent:
            await router.dispatch(intent, ctx)
        else:
            await router.dispatch(Intent("misc", {}), ctx)
        return


    if intent:
        await router.dispatch(intent, ctx)
    else:
        await router.dispatch(Intent("misc", {}), ctx)


# Optional: parity command (kept tiny)
@bot.command(name="members")
async def members(ctx: commands.Context):
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "command",
        "cmd": "members",
        "by": ctx.author.id
    })
    await ctx.send("Members count: (hook up to Members sheet)")

def run():
    bot.run(settings.discord_token)

if __name__ == "__main__":
    run()

