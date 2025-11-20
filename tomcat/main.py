"""Discord bot bootstrap: intents, startup tasks, and router wiring."""

from __future__ import annotations
import asyncio
import time
from typing import Any, Dict, Union
from aiohttp import web

import discord
from discord.ext import commands
from datetime import datetime, timezone

from .config import settings
from .logger import log_event, log_action  # noqa: F401  # imported for shared use
from .intent_router import IntentRouter, Intent
from .handlers.misc import handle_channel_image_intake as _handle_image_intake, start_profile_scheduler
from .services.show_cache import warm_cache_on_boot
from .services.profile_cache import start_profile_cache_scheduler


intent_router = IntentRouter()

SPAM_ALERTS: Dict[int, Dict[str, int]] = {}

# ------- Discord intents & bot -------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(command_prefix=settings.command_prefix, intents=intents)

# ------- Import real handlers -------
# Cats / Feeding and Dues handlers already use the (intent, ctx) signature
from .handlers.cats import handle_cat_show as _handle_cat_show, handle_cat_photo as _handle_cat_photo
from .handlers.feeding import start_feeding_scheduler, handle_feeding_inquiry as _handle_feeding_status
# Dues: no background scheduler; admin-only Gmail test is routed directly from the router
from .handlers.dues import start_gmail_logging_scheduler, start_dues_scheduler

from .handlers.admin import handle_silent_mode as _handle_silent_mode_raw
from .handlers.misc import handle_misc as _handle_misc_raw

from .handlers.vision import handle_cv_detect, handle_cv_crop, handle_cv_identify


# --- Muted wrappers: run handlers but drop outbound sends ---
class _MuteChannel:
    """Proxy channel object that logs outbound messages instead of sending."""
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

    def __getattr__(self, name):
        # Delegate unknown attributes/methods to the real channel
        return getattr(self._real, name)

class _MuteMessage:
    """Lightweight message proxy used when silent mode blocks replies."""
    def __init__(self, real_msg, muted_channel):
        # Keep attributes handlers touch; forward everything else if needed
        self._real = real_msg
        self.channel = muted_channel
        self.author = real_msg.author
        # Preserve common identifiers used by router/handlers
        self.id = getattr(real_msg, "id", None)
        self.guild = getattr(real_msg, "guild", None)
        self.content = real_msg.content
        self.clean_content = getattr(real_msg, "clean_content", self.content)
        self.attachments = getattr(real_msg, "attachments", [])

    def __getattr__(self, name):
        # Delegate any other attributes to the real discord.Message
        return getattr(self._real, name)


async def _handle_misc_adapter(intent: Intent, ctx: Dict[str, Any]) -> None:
    """Bridge router intent signature into the legacy misc handler."""
    message: discord.Message = ctx["message"]
    await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)


def _user_label(u: Union[discord.Member, discord.User]) -> str:
    """Return a human readable label for logging/alerts."""
    return getattr(u, "name", "unknown")



def _channel_label(ch: discord.abc.Messageable) -> str:
    """Pretty-print channels/threads for logs and muted output."""
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
    # Deprecated placeholder; kept for compatibility if referenced elsewhere
    pass

# Admin handler expects (args, ctx) where args == intent.data
async def handle_silent_mode(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_silent_mode_raw(intent.data, ctx)

# Misc handler expects (message, *, now_ts, allow_in_channels)
async def handle_misc(intent: Intent, ctx: Dict[str, Any]) -> None:
    message: discord.Message = ctx["message"]
    await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)

async def handle_cat_profile(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_cat_show(intent, ctx)

async def handle_cat_photo(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_cat_photo(intent, ctx)



#invites_cache
message_cache: dict[int, str] = {}

async def _refresh_invites(guild: discord.Guild):
    """Refreshes the invite cache for a given guild."""
    if not guild.me.guild_permissions.manage_guild:
        print(f"Warning: Missing 'Manage Server' permission in '{guild.name}' to track invites.")
        return
    invites = await guild.invites()
    invites_cache[guild.id] = {inv.code: inv.uses or 0 for inv in invites}

#TomCatUI
async def start_web_server(bot):
    """Simple web server to serve the UI and API."""
    app = web.Application()

    async def get_index(request):
        """Serve the index.html file."""
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html")
        except FileNotFoundError:
            return web.Response(text="index.html not found. Please upload it to the bot root.", status=404)

    async def get_members(request):
        """Return JSON list of members with the Feeding Team role."""
        FEEDING_TEAM_ROLE_ID = 643587274797481988
        found_members = []
        
        for guild in bot.guilds:
            role = guild.get_role(FEEDING_TEAM_ROLE_ID)
            if role:
                for member in role.members:
                    # Use display_name (nickname) if available, fallback to username
                    name = member.display_name
                    found_members.append({
                        "name": name,
                        "user": member.name,
                        "id": str(member.id),
                        "color": str(member.color) if member.color else "#000000"
                    })

        # Deduplicate by ID in case multiple guilds are involved
        unique_members = {m['id']: m for m in found_members}.values()
        # Sort alphabetically
        sorted_members = sorted(unique_members, key=lambda x: x['name'].lower())
        
        return web.json_response(list(sorted_members))

    app.add_routes([
        web.get('/', get_index),
        web.get('/api/members', get_members)
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    # Listen on all interfaces at port 8080
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    print("[TomCat-UI] Web server starting on http://localhost:8080")
    await site.start()


# ------- Lifecycle -------
@bot.event
async def on_ready():
    """Discord callback fired once the bot connects."""
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
    asyncio.create_task(start_web_server(bot))
    asyncio.create_task(start_profile_scheduler(bot))
    # Warm the show-photo cache in background
    try:
        asyncio.create_task(warm_cache_on_boot())
    except Exception:
        pass
    # start feeding scheduler after the bot is ready and loop is running
    asyncio.create_task(start_feeding_scheduler(bot))
    # Start Gmail logging scheduler if enabled
    try:
        if getattr(settings, "gmail_enabled", False):
            asyncio.create_task(start_gmail_logging_scheduler(bot))
        if getattr(settings, "dues_enabled", True):
            asyncio.create_task(start_dues_scheduler(bot))
    except Exception:
        pass
    # Start catabase profile cache scheduler
    try:
        asyncio.create_task(start_profile_cache_scheduler())
    except Exception:
        pass


# ------- Message entrypoint -------
@bot.event
async def on_message(message: discord.Message):
    """Main message hook: run anti-spam and route intents."""
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
                        f"{mention}\n"
                        "Click the ❌ reaction below to ban this user."
                    ).strip()
                    alert_msg = None
                    if getattr(settings, "silent_mode", False):
                        snippet = body.replace("\n", " ")[:120]
                        log_action("send_suppressed", f"ch={getattr(ch,'id',None)}", snippet)
                    else:
                        try:
                            alert_msg = await ch.send(body)
                        except Exception as send_exc:
                            log_action("spam_alert_error", f"ch={getattr(ch,'id',None)}", str(send_exc))
                    if alert_msg:
                        try:
                            await alert_msg.add_reaction('❌')
                            target_id = int(getattr(message.author, 'id', 0) or 0)
                            guild_id = int(getattr(message.guild, 'id', 0) or 0)
                            if target_id:
                                SPAM_ALERTS[alert_msg.id] = {"user_id": target_id, "guild_id": guild_id}
                        except Exception as react_exc:
                            log_action("spam_alert_react_error", "add_reaction", str(react_exc))
        except Exception:
            pass
        return
    # Channel/DM → Sheet image intake
    try:
        if getattr(message, "attachments", None):
            in_map = settings.channel_sheet_map and int(getattr(message.channel, "id", 0) or 0) in settings.channel_sheet_map
            is_dm = getattr(message, "guild", None) is None
            if in_map or is_dm:
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

    await intent_router.handle_message(message, ctx)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Handle reaction-based workflows (feeding + spam ban)."""
    if payload.user_id == getattr(bot.user, 'id', None):
        return
    data = SPAM_ALERTS.get(payload.message_id)
    if not data:
        return
    emoji_str = str(payload.emoji)
    if emoji_str not in {'❌', '✖', '✖️'}:
        return
    guild_id = payload.guild_id or data.get('guild_id', 0)
    guild = bot.get_guild(guild_id) if guild_id else None
    if not guild:
        return
    # Prevent the accused user from banning themselves via reaction
    if payload.user_id == data.get('user_id'):
        return
    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except Exception:
        member = None
    if not member:
        return
    admin_ids = {int(x) for x in (getattr(settings, 'admin_ids', []) or [])}
    can_ban = False
    if payload.user_id in admin_ids:
        can_ban = True
    else:
        ban_role_tokens = [s.lower() for s in (getattr(settings, 'spam_ban_role_names', []) or [])]
        for role in getattr(member, 'roles', []) or []:
            rname = str(getattr(role, 'name', '')).lower()
            if any(tok in rname for tok in ban_role_tokens):
                can_ban = True
                break
    if not can_ban:
        return
    target_id = data.get('user_id')
    if not target_id:
        return
    try:
        target_member = guild.get_member(target_id) or await guild.fetch_member(target_id)
    except Exception:
        target_member = None
    try:
        if target_member:
            await guild.ban(target_member, reason="Spam reaction ban")
        else:
            await guild.ban(discord.Object(id=target_id), reason="Spam reaction ban")
        log_action('spam_ban', f"guild={guild_id}", f"user={target_id}")
        SPAM_ALERTS.pop(payload.message_id, None)
    except Exception as ban_exc:
        log_action('spam_ban_error', f"guild={guild_id}", str(ban_exc))


# ------- Edit/Delete logging -------
@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Re-run router when users edit messages."""
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
    """Log deleted messages for moderation context."""
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
    """Track invite usage and log onboarding events."""
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
    """Log when members leave so invite stats stay accurate."""
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
    """Update the invite cache when new invites appear."""
    try:
        g = invite.guild
        if g:
            await _refresh_invites(g)
    except Exception:
        pass

@bot.event
async def on_invite_delete(invite: discord.Invite):
    """Remove cached invites when Discord deletes them."""
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
    """Keep spam alert reactions tidy when moderators undo them."""
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
