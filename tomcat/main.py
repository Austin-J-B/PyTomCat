from __future__ import annotations
import asyncio
import time
from typing import Any, Dict, Union

import discord
from discord.ext import commands
from datetime import datetime, timezone

from .config import settings
from .logger import log_event, log_action  # noqa: F401  #If unused right now
from .spam import is_spam
from .intent_router import IntentRouter, Intent
from .handlers.misc import handle_channel_image_intake as _handle_image_intake, start_profile_scheduler


intent_router = IntentRouter()

# ------- Discord intents & bot -------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(command_prefix=settings.command_prefix, intents=intents)

# ------- Import real handlers -------
# Cats / Feeding and Dues already match (intent, ctx) in your tree
from .handlers.cats import handle_cat_show as _handle_cat_show, handle_cat_photo as _handle_cat_photo
from .handlers.feeding import start_feeding_scheduler, handle_feeding_inquiry as _handle_feeding_status
from .handlers.dues import (
    handle_dues_notice as _handle_dues_notice,
    process_dues_cycle as _process_dues_cycle,
    init_db as _init_db,
)

from .handlers.admin import handle_silent_mode as _handle_silent_mode_raw
from .handlers.misc import handle_misc as _handle_misc_raw

from .handlers.vision import handle_cv_detect, handle_cv_crop, handle_cv_identify


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
        return "DM"
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

async def handle_cat_photo(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_cat_photo(intent, ctx)



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

    # Startup health checks (file logs only)
    async def _health_checks():
        try:
            # Check image intake tabs
            from .handlers.misc import _open_ws as _open_ws_misc
            for ch_id, tab in (settings.channel_sheet_map or {}).items():
                try:
                    ws = _open_ws_misc(tab)
                    if ws:
                        log_event({"event":"health","component":"image_tab","status":"ok","channel_id": ch_id, "tab": tab})
                    else:
                        log_event({"event":"health","component":"image_tab","status":"missing","channel_id": ch_id, "tab": tab})
                except Exception as e:
                    log_event({"event":"health","component":"image_tab","status":"error","channel_id": ch_id, "tab": tab, "error": str(e)})
        except Exception as e:
            log_event({"event":"health","component":"image_tab","status":"error","error": str(e)})
        try:
            # Check feeding checklist tab
            from .handlers.feeding import _open_feeding_ws
            ws = _open_feeding_ws()
            if ws:
                log_event({"event":"health","component":"feeding_tab","status":"ok"})
            else:
                log_event({"event":"health","component":"feeding_tab","status":"missing"})
        except Exception as e:
            log_event({"event":"health","component":"feeding_tab","status":"error","error": str(e)})

    asyncio.create_task(_health_checks())

    # Seed invite caches for all guilds (for join attribution)
    try:
        for g in bot.guilds:
            try:
                await _refresh_invites(g)
            except Exception:
                pass
    except Exception:
        pass

    async def _dues_loop():
        while True:
            try:
                await _process_dues_cycle(bot)
            except Exception as e:
                log_event({"event": "dues_loop_error", "error": str(e)})
            await asyncio.sleep(7200)
    asyncio.create_task(start_profile_scheduler(bot))
    # start feeding scheduler after the bot is ready and loop is running
    asyncio.create_task(start_feeding_scheduler(bot))
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

    # Spam protection (text + heuristics + NLP backstop for new/untrusted accounts)
    from .spam import check_spam
    spam_flag, reason = check_spam(message, settings)
    if spam_flag:
        # Log and notify in logging channel, then delete the message
        try:
            # Delete spam message (best-effort)
            try:
                await message.delete()
                decision = "deleted"
            except Exception:
                decision = "kept"

            # Write log line
            log_event({
                "event": "spam",
                "user": _user_label(message.author),
                "channel": _channel_label(message.channel),
                "content": message.clean_content if isinstance(message.content, str) else "",
                "decision": decision,
                "reason": reason,
            })

            # Notify moderators in CH_LOGGING
            log_ch_id = getattr(settings, 'ch_logging', None)
            if log_ch_id:
                ch = message.guild.get_channel(int(log_ch_id)) if message.guild else None
                if not ch:
                    ch = bot.get_channel(int(log_ch_id))
                if ch and hasattr(ch, 'send'):
                    alert_uid = getattr(settings, 'spam_alert_user_id', None) or (getattr(settings, 'admin_ids', []) or [None])[0]
                    mention = f"<@{int(alert_uid)}>" if alert_uid else ""
                    uname = f"@{getattr(message.author,'name','unknown-user')}"
                    body = (
                        "Spam Message Detected\n"
                        f"User: {uname} ({getattr(message.author,'id','')})\n"
                        "Message:\n"
                        f"{message.content or ''}\n\n"
                        f"{mention}"
                    ).strip()
                    from .utils.sender import safe_send
                    await safe_send(ch, body)
        except Exception:
            pass
        return
    # Channel → Sheet image intake (unprompted, only in mapped channels)
    try:
        if getattr(message, "attachments", None) and settings.channel_sheet_map and int(message.channel.id) in settings.channel_sheet_map:
            await _handle_image_intake(message)
    except Exception as e:
        log_action("image_intake_error", f"channel={getattr(message.channel,'id','?')}", str(e))

    # Lightweight fun triggers (e.g., "meow") anywhere; safe_send respects silent mode
    try:
        await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)
    except Exception:
        pass

    # Build ctx once
    ctx: Dict[str, Any] = {
        "bot": bot,
        "message": message,
        "channel": message.channel,
        "author": message.author,
    }

    # Global mute: while silent_mode is ON, route everything through a MuteChannel/Message
    if settings.silent_mode:
        muted_ch = _MuteChannel(message.channel, _channel_label)
        muted_msg = _MuteMessage(message, muted_ch)
        ctx["channel"] = muted_ch
        ctx["message"] = muted_msg
        await intent_router.handle_message(muted_msg, ctx)
        return

    # Normal path
    await intent_router.handle_message(message, ctx)


# ------- Edit/Delete logging -------
@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    try:
        if before.author.bot:
            return
        log_event({
            "event": "message_edit",
            "author": _user_label(before.author),
            "channel": _channel_label(before.channel),
            "before": before.clean_content if isinstance(before.content, str) else "",
            "after": after.clean_content if isinstance(after.content, str) else "",
        })
    except Exception:
        pass

@bot.event
async def on_message_delete(message: discord.Message):
    try:
        if message.author and message.author.bot:
            return
        log_event({
            "event": "message_delete",
            "author": _user_label(getattr(message, 'author', type('X', (), {'name':'unknown'})())),
            "channel": _channel_label(getattr(message, 'channel', type('Y', (), {'name':'unknown'})())),
            "content": message.clean_content if isinstance(getattr(message, 'content', None), str) else "",
        })
    except Exception:
        pass


# ------- Member join/leave + invite tracking -------
@bot.event
async def on_member_join(member: discord.Member):
    try:
        guild = member.guild
        # Compute account age in days
        created = getattr(member, 'created_at', None)
        from datetime import timezone
        age_days = None
        if created:
            try:
                now = datetime.now(timezone.utc)
                age_days = (now - created).days
            except Exception:
                age_days = None

        # Detect which invite increased
        code_used = None
        inviter_id = None
        try:
            before = invites_cache.get(guild.id, {})
            invites = await guild.invites()
            after = {inv.code: (inv.uses or 0) for inv in invites}
            for inv in invites:
                b = before.get(inv.code, 0)
                a = after.get(inv.code, 0)
                if a > b:
                    code_used = inv.code
                    inviter_id = getattr(inv.inviter, 'id', None)
                    break
            invites_cache[guild.id] = after
        except Exception:
            pass

        log_event({
            "event": "member_join",
            "user": _user_label(member),
            "user_id": int(getattr(member, 'id', 0)),
            "guild": getattr(guild, 'name', ''),
            "guild_id": int(getattr(guild, 'id', 0)),
            "account_age_days": age_days,
            "invite_code": code_used,
            "inviter_id": inviter_id,
        })
    except Exception:
        pass

@bot.event
async def on_member_remove(member: discord.Member):
    try:
        log_event({
            "event": "member_leave",
            "user": _user_label(member),
            "user_id": int(getattr(member, 'id', 0)),
            "guild": getattr(member.guild, 'name', ''),
            "guild_id": int(getattr(member.guild, 'id', 0))
        })
    except Exception:
        pass

@bot.event
async def on_invite_create(invite: discord.Invite):
    try:
        g = invite.guild
        if g:
            await _refresh_invites(g)
    except Exception:
        pass

@bot.event
async def on_invite_delete(invite: discord.Invite):
    try:
        g = invite.guild
        if g:
            await _refresh_invites(g)
    except Exception:
        pass


# ------- Reactions and role changes logging -------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    try:
        # Ignore bot reactions
        if payload.user_id == getattr(bot.user, 'id', None):
            return
        ch = bot.get_channel(int(payload.channel_id))
        msg = None
        preview = ""
        author_name = ""
        if ch and hasattr(ch, 'fetch_message'):
            try:
                msg = await ch.fetch_message(int(payload.message_id))
                content = msg.clean_content if isinstance(getattr(msg, 'content', None), str) else ""
                preview = content[:40] + ("..." if len(content) > 40 else "")
                author_name = _user_label(getattr(msg, 'author', None))
            except Exception:
                pass
        log_event({
            "event": "reaction_add",
            "user": _user_label(getattr(payload, 'member', None)) or str(payload.user_id),
            "channel": _channel_label(ch) if ch else str(payload.channel_id),
            "message_id": int(payload.message_id),
            "emoji": str(payload.emoji),
            "message_preview": preview,
            "message_author": author_name,
        })
    except Exception:
        pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    try:
        ch = bot.get_channel(int(payload.channel_id))
        msg = None
        preview = ""
        author_name = ""
        if ch and hasattr(ch, 'fetch_message'):
            try:
                msg = await ch.fetch_message(int(payload.message_id))
                content = msg.clean_content if isinstance(getattr(msg, 'content', None), str) else ""
                preview = content[:40] + ("..." if len(content) > 40 else "")
                author_name = _user_label(getattr(msg, 'author', None))
            except Exception:
                pass
        log_event({
            "event": "reaction_remove",
            "user": _user_label(getattr(payload, 'member', None)) or str(payload.user_id),
            "channel": _channel_label(ch) if ch else str(payload.channel_id),
            "message_id": int(payload.message_id),
            "emoji": str(payload.emoji),
            "message_preview": preview,
            "message_author": author_name,
        })
    except Exception:
        pass

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    try:
        # Compare role IDs
        before_ids = {int(r.id) for r in getattr(before, 'roles', [])}
        after_ids = {int(r.id) for r in getattr(after, 'roles', [])}
        added_ids = list(after_ids - before_ids)
        removed_ids = list(before_ids - after_ids)
        if not added_ids and not removed_ids:
            return
        def _names(ids):
            out = []
            for rid in ids:
                role = after.guild.get_role(rid)
                out.append(getattr(role, 'name', str(rid)))
            return out
        log_event({
            "event": "member_update",
            "user": _user_label(after),
            "user_id": int(getattr(after,'id',0)),
            "guild": getattr(after.guild, 'name', ''),
            "roles_added": _names(added_ids),
            "roles_removed": _names(removed_ids),
        })
    except Exception:
        pass

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
