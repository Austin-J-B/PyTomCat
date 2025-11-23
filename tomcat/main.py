"""Discord bot bootstrap: intents, startup tasks, and router wiring."""

from __future__ import annotations
import asyncio
import time
import json
import aiohttp
from typing import Any, Dict, Union
from aiohttp import web

import discord
from discord.ext import commands
from datetime import datetime, timezone

from .config import settings
from .logger import log_event, log_action
from .intent_router import IntentRouter, Intent
from .handlers.misc import handle_channel_image_intake as _handle_image_intake, start_profile_scheduler
from .services.show_cache import warm_cache_on_boot
from .services.profile_cache import start_profile_cache_scheduler
from .services.scheduler_store import load_schedule, save_schedule

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
from .handlers.cats import handle_cat_show as _handle_cat_show, handle_cat_photo as _handle_cat_photo
from .handlers.feeding import start_feeding_scheduler, handle_feeding_inquiry as _handle_feeding_status
from .handlers.dues import start_gmail_logging_scheduler, start_dues_scheduler
from .handlers.admin import handle_silent_mode as _handle_silent_mode_raw
from .handlers.misc import handle_misc as _handle_misc_raw
from .handlers.vision import handle_cv_detect, handle_cv_crop, handle_cv_identify

# --- Muted wrappers (omitted for brevity, same as before) ---
class _MuteChannel:
    def __init__(self, real, label_fn):
        self._real = real
        self._label_fn = label_fn
        self.id = getattr(real, "id", None)
    async def send(self, content=None, **kwargs):
        log_action("muted_send", f"channel={self._label_fn(self._real)}", "suppressed")
        return None
    def __getattr__(self, name): return getattr(self._real, name)

class _MuteMessage:
    def __init__(self, real_msg, muted_channel):
        self._real = real_msg
        self.channel = muted_channel
        self.author = real_msg.author
        self.content = real_msg.content
        self.guild = getattr(real_msg, "guild", None)
    def __getattr__(self, name): return getattr(self._real, name)

async def handle_cat_show(intent: Intent, ctx: Dict[str, Any]) -> None: await _handle_cat_show(intent, ctx)
async def handle_feeding_status(intent: Intent, ctx: Dict[str, Any]) -> None: await _handle_feeding_status(intent, ctx)
async def handle_dues_notice(intent: Intent, ctx: Dict[str, Any]) -> None: pass
async def handle_silent_mode(intent: Intent, ctx: Dict[str, Any]) -> None: await _handle_silent_mode_raw(intent.data, ctx)
async def handle_misc(intent: Intent, ctx: Dict[str, Any]) -> None: await _handle_misc_raw(ctx["message"], now_ts=time.time(), allow_in_channels=None)
async def handle_cat_profile(intent: Intent, ctx: Dict[str, Any]) -> None: await _handle_cat_show(intent, ctx)
async def handle_cat_photo(intent: Intent, ctx: Dict[str, Any]) -> None: await _handle_cat_photo(intent, ctx)

def _user_label(u) -> str: return getattr(u, "name", "unknown")
def _channel_label(ch) -> str: return getattr(ch, "name", "unknown")

message_cache: dict[int, str] = {}
invites_cache: dict[int, dict[str, int]] = {}

async def _refresh_invites(guild: discord.Guild):
    if not guild.me.guild_permissions.manage_guild: return
    invites = await guild.invites()
    invites_cache[guild.id] = {inv.code: inv.uses or 0 for inv in invites}

# --- TOMCAT UI & API ---
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
}

async def check_permissions(user_id: int) -> dict:
    """
    Determines what the user can do based on config.py rules.
    We check the officer role in ANY guild the bot shares with the user.
    """
    is_officer = False
    
    # Search for user in all guilds
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member:
            # Check for officer role ID
            for role in member.roles:
                if role.id == settings.role_officer_id:
                    is_officer = True
                    break
        if is_officer: break
    
    # ACL Checks
    can_manage_feeding = is_officer or (user_id in settings.access_feeding_manager)
    can_label_photos = is_officer or (user_id in settings.access_photo_labeler)
    
    return {
        "can_manage_feeding": can_manage_feeding,
        "can_label_photos": can_label_photos
    }

async def start_web_server(bot):
    app = web.Application()

    # 1. Serve HTML
    async def get_index(request):
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html", headers=_CORS_HEADERS)
        except FileNotFoundError:
            return web.Response(text="index.html not found.", status=404)

    # 2. Auth Exchange
    async def post_auth(request):
        try:
            data = await request.json()
            code = data.get("code")
            if not code: return web.Response(status=400, text="Missing code")

            # Exchange with Discord
            async with aiohttp.ClientSession() as session:
                payload = {
                    'client_id': settings.ui_activity_app_id,
                    'client_secret': settings.discord_client_secret,
                    'grant_type': 'authorization_code',
                    'code': code,
                }
                async with session.post('https://discord.com/api/oauth2/token', data=payload) as resp:
                    if resp.status != 200:
                        txt = await resp.text()
                        print(f"Auth failed: {txt}")
                        return web.Response(status=401, text="Discord auth failed")
                    token_data = await resp.json()
                    access_token = token_data['access_token']

                # Get User Info
                async with session.get('https://discord.com/api/users/@me', headers={'Authorization': f'Bearer {access_token}'}) as user_resp:
                    user_info = await user_resp.json()
                    user_id = int(user_info['id'])
            
            # Calculate Permissions
            perms = await check_permissions(user_id)
            
            return web.json_response({
                "user": user_info,
                "permissions": perms,
                # In a real app, we'd mint a JWT here. For simplicity, we rely on the client sending their ID 
                # and we re-verify roles on save, or we trust the ephemeral nature of the Activity session.
                # SECURITY NOTE: This simple demo returns the User ID to client. 
                # Real production apps should sign this data.
            }, headers=_CORS_HEADERS)
            
        except Exception as e:
            print(f"Auth error: {e}")
            return web.Response(status=500, text=str(e))

    # 3. Get Schedule
    async def get_schedule(request):
        data = load_schedule()
        return web.json_response(data, headers=_CORS_HEADERS)

    # 4. Save Schedule (Protected)
    async def post_schedule_save(request):
        try:
            # Simple Auth Check: Header 'X-User-ID'
            # Note: In a high-security env, this should be a signed session token.
            # Since this is a Discord Activity, spoofing is harder but possible if user extracts URL.
            # For now, we trust the flow but re-verify permissions.
            
            user_id_str = request.headers.get("X-User-ID")
            if not user_id_str:
                return web.Response(status=401, text="Unauthorized")
            
            user_id = int(user_id_str)
            perms = await check_permissions(user_id)
            
            if not perms["can_manage_feeding"]:
                return web.Response(status=403, text="You are not a Feeding Manager.")
            
            data = await request.json()
            # Save to disk
            if save_schedule(data):
                return web.Response(text="Saved", headers=_CORS_HEADERS)
            else:
                return web.Response(status=500, text="Write failed")
                
        except Exception as e:
            print(f"Save error: {e}")
            return web.Response(status=500, text=str(e))

    async def handle_options(request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    app.add_routes([
        web.get('/', get_index),
        web.post('/api/auth/token', post_auth),
        web.get('/api/schedule', get_schedule),
        web.post('/api/schedule/save', post_schedule_save),
        web.options('/api/auth/token', handle_options),
        web.options('/api/schedule', handle_options),
        web.options('/api/schedule/save', handle_options),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    print("[TomCat-UI] Server active on port 8080")
    await site.start()

# ------- Lifecycle (Same as before) -------
@bot.event
async def on_ready():
    print(f"[TomCat] Logged in as {bot.user}")
    log_event({"event": "online", "user": str(bot.user)})
    
    # Seed tasks
    asyncio.create_task(start_web_server(bot))
    asyncio.create_task(start_profile_scheduler(bot))
    asyncio.create_task(start_feeding_scheduler(bot))
    if getattr(settings, "gmail_enabled", False):
        asyncio.create_task(start_gmail_logging_scheduler(bot))
    if getattr(settings, "dues_enabled", True):
        asyncio.create_task(start_dues_scheduler(bot))
    try: asyncio.create_task(warm_cache_on_boot())
    except: pass
    try: asyncio.create_task(start_profile_cache_scheduler())
    except: pass

@bot.event
async def on_message(message):
    if message.author.bot: return
    log_event({"event": "message", "author": _user_label(message.author), "content": message.clean_content[:50]})
    
    # Spam Check
    from .spam import check_spam
    spam_flag, reason = check_spam(message, settings)
    if spam_flag:
        try: await message.delete()
        except: pass
        # (Spam logging logic omitted for brevity, keep your existing one)
        return

    # Image Intake
    if message.attachments:
        in_map = settings.channel_sheet_map and message.channel.id in settings.channel_sheet_map
        if in_map or not message.guild:
            try: await _handle_image_intake(message)
            except Exception as e: log_action("intake_err", str(message.channel.id), str(e))

    # Router
    ctx = {"bot": bot, "message": message, "channel": message.channel, "author": message.author}
    if settings.silent_mode:
        ctx["channel"] = _MuteChannel(message.channel, _channel_label)
    
    await intent_router.handle_message(message, ctx)

def run():
    bot.run(settings.discord_token)

if __name__ == "__main__":
    run()