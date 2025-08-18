from __future__ import annotations
import discord
from discord.ext import commands
from datetime import datetime, timezone
from .config import settings
from .logger import log_event
from .intents import classify, Intent
from .router import Router
from .handlers.cats import handle_cat_show
from .handlers.feeding import handle_feeding_status
from .handlers.dues import handle_dues_notice
import time
from .handlers.misc import handle_misc
from .scheduler import dues_ingest_loop
from .models.dues_store import init_db

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(command_prefix=settings.command_prefix, intents=intents)
router = Router()
router.register("cat_show", handle_cat_show)
router.register("feeding_status", handle_feeding_status)
router.register("dues_notice", handle_dues_notice)

invites_cache: dict[int, dict[str, int]] = {}  # guild_id -> {code: uses}
message_cache: dict[int, str] = {}

async def _refresh_invites(guild: discord.Guild):
    """Refreshes the invite cache for a given guild."""
    if not guild.me.guild_permissions.manage_guild:
        print(f"Warning: Missing 'Manage Server' permission in '{guild.name}' to track invites.")
        return
    try:
        invites = await guild.invites()
        invites_cache[guild.id] = {
            invite.code: invite.uses for invite in invites if invite.uses is not None
        }
    except discord.Forbidden:
        print(f"Error: Could not fetch invites for '{guild.name}' due to missing permissions.")
    except discord.HTTPException as e:
        print(f"Error: Failed to fetch invites for '{guild.name}': {e}")


@bot.event
async def on_ready():
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "ready",
        "bot": str(bot.user) if bot.user else "Unknown"
    })
    init_db()
    # Refresh invites for all guilds
    for g in bot.guilds:
        await _refresh_invites(g)
    print(f"TomCat VI online as {bot.user}")
    bot.loop.create_task(dues_ingest_loop())

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    now = time.time()
    # Cache message content for edit tracking
    message_cache[message.id] = message.content
    log_event({
        "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "event": "message",
        "id": message.id,
        "author_id": message.author.id,
        "author": message.author.display_name,
        "channel_id": message.channel.id,
        "channel": getattr(message.channel, "name", None),
        "content": message.content,
    })
    await handle_misc(message, now_ts=now, allow_in_channels=settings.misc_channels)
    intent: Intent | None = classify(message.channel.id, message.content)
    if intent:
        log_event({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "intent",
            "type": intent.type,
            "args": intent.args,
            "msg_id": message.id
        })
        await router.dispatch(intent, {"channel": message.channel, "author": message.author, "message": message})
    await bot.process_commands(message)

@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    # Ignore edits from bots to prevent log spam
    if payload.data.get("author", {}).get("bot", False):
        return

    before_content = (
        payload.cached_message.content if payload.cached_message
        else message_cache.get(payload.message_id, "[Content not cached]")
    )
    after_content = payload.data.get("content", "[Content not available]")
    # Update cache with new content if present
    if after_content not in ("[Content not available]", None):
        message_cache[payload.message_id] = after_content
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "message_edit",
        "id": payload.message_id,
        "channel_id": payload.channel_id,
        "before": before_content,
        "after": after_content,
    })

@bot.event
async def on_message_delete(message: discord.Message):
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "message_delete",
        "id": message.id,
        "channel_id": message.channel.id
    })

@bot.event
async def on_member_join(member: discord.Member):
    before = invites_cache.get(member.guild.id, {})
    await _refresh_invites(member.guild)
    after = invites_cache.get(member.guild.id, {})
    
    used_invite_code = None
    for code, uses in after.items():
        if uses > before.get(code, 0):
            used_invite_code = code
            break
            
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "member_join",
        "id": member.id,
        "name": member.display_name,
        "invite_code": used_invite_code
    })

@bot.event
async def on_member_remove(member: discord.Member):
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "member_remove",
        "id": member.id,
        "name": member.display_name
    })

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    user = await bot.fetch_user(payload.user_id)
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "reaction_add",
        "user_id": payload.user_id,
        "user_name": user.display_name if user else "Unknown User",
        "message_id": payload.message_id,
        "emoji": str(payload.emoji),
        "channel_id": payload.channel_id,
    })

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    user = await bot.fetch_user(payload.user_id)
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "reaction_remove",
        "user_id": payload.user_id,
        "user_name": user.display_name if user else "Unknown User",
        "message_id": payload.message_id,
        "emoji": str(payload.emoji),
        "channel_id": payload.channel_id,
    })

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
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN missing in .env")
    bot.run(settings.discord_token)

if __name__ == "__main__":
    run()

