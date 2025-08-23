from __future__ import annotations
import asyncio, time
import discord
from discord.ext import commands
from datetime import datetime, timezone
from .config import settings
from .logger import log_event, log_action
from .intents import classify, Intent
from .router import Router
from .handlers.cats import handle_cat_show
from .handlers.feeding import handle_feeding_status
from .handlers.dues import handle_dues_notice, process_dues_cycle, init_db
from .handlers.admin import handle_silent_mode
from .handlers.misc import handle_misc
from .utils.sender import safe_send
from .spam import is_spam

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
router.register("silent_mode", handle_silent_mode)

invites_cache: dict[int, dict[str, int]] = {}
message_cache: dict[int, str] = {}

async def _refresh_invites(guild: discord.Guild):
    if not guild.me.guild_permissions.manage_guild:
        return
    try:
        invites = await guild.invites()
        invites_cache[guild.id] = {i.code: i.uses for i in invites if i.uses is not None}
    except Exception:
        return

@bot.event
async def on_ready():
    log_event({"ts": datetime.now(timezone.utc).isoformat(), "event": "ready", "bot": str(bot.user)})
    init_db()
    for g in bot.guilds:
        await _refresh_invites(g)
    async def _dues_loop():
        while True:
            await process_dues_cycle(bot)
            await asyncio.sleep(7200)
    bot.loop.create_task(_dues_loop())

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    now = time.time()
    message_cache[message.id] = message.content
    human_line = log_event({
        "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "event": "message",
        "id": message.id,
        "author_id": message.author.id,
        "author": message.author.display_name,
        "channel_id": message.channel.id,
        "channel": getattr(message.channel, "name", None),
        "content": message.content,
    })
    if settings.ch_logging and not settings.silent_mode:
        ch = bot.get_channel(settings.ch_logging)
        if ch:
            await safe_send(ch, human_line)
    if is_spam(message.content):
        try:
            await message.delete()
        except Exception:
            pass
        line = log_action("spam_removed", f"user={message.author.id}", message.content)
        if settings.ch_logging and not settings.silent_mode:
            ch = bot.get_channel(settings.ch_logging)
            if ch:
                await safe_send(ch, line)
        return
    await handle_misc(message, now_ts=now, allow_in_channels=settings.misc_channels)
    intent: Intent | None = classify(message.channel.id, message.content)
    if intent:
        log_event({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "intent",
            "type": intent.type,
            "args": intent.args,
            "msg_id": message.id,
        })
        await router.dispatch(intent, {"channel": message.channel, "author": message.author, "message": message})
    await bot.process_commands(message)

@bot.command(name="members")
async def members(ctx: commands.Context):
    await ctx.send("Members count: (hook up to Members sheet)")

@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.data.get("author", {}).get("bot", False):
        return
    before_content = (
        payload.cached_message.content if payload.cached_message else message_cache.get(payload.message_id, "")
    )
    after_content = payload.data.get("content", "")
    if after_content:
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
        "channel_id": message.channel.id,
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
        "invite_code": used_invite_code,
    })

@bot.event
async def on_member_remove(member: discord.Member):
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "member_remove",
        "id": member.id,
        "name": member.display_name,
    })

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    user = await bot.fetch_user(payload.user_id)
    log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "reaction_add",
        "user_id": payload.user_id,
        "user_name": user.display_name if user else "Unknown",
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
        "user_name": user.display_name if user else "Unknown",
        "message_id": payload.message_id,
        "emoji": str(payload.emoji),
        "channel_id": payload.channel_id,
    })

def run():
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN missing in .env")
    bot.run(settings.discord_token)

if __name__ == "__main__":
    run()
