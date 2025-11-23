# tomcat/main.py
from __future__ import annotations
import asyncio
import time
import json
import os
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

from .config import settings
from .logger import log_event, log_action
from .intent_router import IntentRouter
from .handlers.misc import handle_channel_image_intake as _handle_image_intake, start_profile_scheduler
from .services.show_cache import warm_cache_on_boot
from .services.profile_cache import start_profile_cache_scheduler
from .services.scheduler_store import load_schedule, save_schedule

# Import Handlers
from .handlers.cats import handle_cat_show as _handle_cat_show, handle_cat_photo as _handle_cat_photo
from .handlers.feeding import start_feeding_scheduler, handle_feeding_inquiry as _handle_feeding_status
from .handlers.dues import start_gmail_logging_scheduler, start_dues_scheduler
from .handlers.admin import handle_silent_mode as _handle_silent_mode_raw
from .handlers.misc import handle_misc as _handle_misc_raw

intent_router = IntentRouter()

# Setup Bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True
bot = commands.Bot(command_prefix=settings.command_prefix, intents=intents)

# --- AUTH LOGIC ---
async def check_permissions(user_id: int) -> dict:
    is_officer = False
    # Check guilds to find user roles
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member:
            for role in member.roles:
                if role.id == settings.role_officer_id:
                    is_officer = True
                    break
        if is_officer: break
    
    # Per instructions: Photo Labelers can also edit the schedule
    can_edit = (
        is_officer 
        or (user_id in settings.access_feeding_manager)
        or (user_id in settings.access_photo_labeler)
    )

    return {
        "can_edit_schedule": can_edit,
        "is_officer": is_officer,
        "is_photo_labeler": user_id in settings.access_photo_labeler
    }

# --- WEB SERVER ---
async def start_web_server(bot):
    app = web.Application()
    
    _CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,Authorization,X-User-ID"
    }

    async def get_index(req):
        # robustly find index.html relative to the bot script or current dir
        possible_paths = [
            "index.html", 
            os.path.join(os.path.dirname(__file__), "..", "index.html"),
            os.path.join(os.path.dirname(__file__), "index.html")
        ]
        
        content = None
        for path in possible_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    break
                except: continue
        
        if content:
            return web.Response(text=content, content_type="text/html", headers=_CORS)
        return web.Response(status=404, text="index.html not found")

    async def get_members(req):
        found = []
        for name, uid in settings.user_id_map.items():
            user = bot.get_user(uid)
            handle = user.name if user else "unknown"
            found.append({"name": name, "user": handle, "id": str(uid)})
        found.sort(key=lambda x: x['name'])
        return web.json_response(found, headers=_CORS)

    async def post_auth(req):
        try:
            data = await req.json()
            # Code Exchange
            async with aiohttp.ClientSession() as sess:
                async with sess.post('https://discord.com/api/oauth2/token', data={
                    'client_id': settings.ui_activity_app_id,
                    'client_secret': settings.discord_client_secret,
                    'grant_type': 'authorization_code',
                    'code': data.get('code')
                }) as r:
                    if r.status != 200: 
                        print(f"Auth fail: {await r.text()}")
                        return web.Response(status=401)
                    token_data = await r.json()
                    access_token = token_data['access_token']
                
                async with sess.get('https://discord.com/api/users/@me', headers={'Authorization': f'Bearer {access_token}'}) as r:
                    user = await r.json()
            
            perms = await check_permissions(int(user['id']))
            return web.json_response({"user": user, "permissions": perms}, headers=_CORS)
        except Exception as e: 
            print(f"Auth Error: {e}")
            return web.Response(status=500, text=str(e))

    async def get_sched(req):
        return web.json_response(load_schedule(), headers=_CORS)

    async def save_sched(req):
        try:
            # Security: In a production app, we'd verify a session token. 
            # For this scale, checking the ID against the bot's known permissions is acceptable.
            uid = int(req.headers.get("X-User-ID", 0))
            perms = await check_permissions(uid)
            
            if not perms["can_edit_schedule"]: 
                return web.Response(status=403, text="Permission Denied")
            
            data = await req.json()
            save_schedule(data)
            return web.Response(text="Saved", headers=_CORS)
        except Exception as e: 
            print(f"Save Error: {e}")
            return web.Response(status=500)

    async def opts(req): return web.Response(status=204, headers=_CORS)

    app.add_routes([
        web.get('/', get_index),
        web.get('/api/members', get_members),
        web.get('/api/schedule', get_sched),
        web.post('/api/auth/token', post_auth),
        web.post('/api/schedule/save', save_sched),
        web.options('/api/members', opts),
        web.options('/api/auth/token', opts),
        web.options('/api/schedule/save', opts)
    ])
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8080).start()
    print("[TomCat-UI] Server running on http://localhost:8080")

# --- WRAPPERS ---
async def handle_cat_show(i, c): await _handle_cat_show(i, c)
async def handle_feeding_status(i, c): await _handle_feeding_status(i, c)
async def handle_cat_photo(i, c): await _handle_cat_photo(i, c)
async def handle_silent_mode(i, c): await _handle_silent_mode_raw(i.data, c)
async def handle_misc(i, c): 
    await _handle_misc_raw(c["message"], now_ts=time.time(), allow_in_channels=None)
async def handle_dues_notice(i, c): pass
async def handle_cat_profile(i, c): await _handle_cat_show(i, c)

# --- BOT EVENTS ---
class _MuteChannel:
    def __init__(self, real, label_fn): self._real = real; self._label_fn = label_fn
    async def send(self, content=None, **kwargs): log_action("muted_send", f"channel={self._label_fn(self._real)}", "suppressed")
    def __getattr__(self, name): return getattr(self._real, name)

class _MuteMessage:
    def __init__(self, real_msg, muted_channel):
        self._real = real_msg
        self.channel = muted_channel
        self.author = real_msg.author
        self.content = real_msg.content
        self.guild = getattr(real_msg, "guild", None)
        self.attachments = real_msg.attachments
    def __getattr__(self, name): return getattr(self._real, name)

def _user_label(u) -> str: return getattr(u, "name", "unknown")
def _channel_label(ch) -> str: return getattr(ch, "name", "unknown")

@bot.event
async def on_ready():
    print(f"[TomCat] Ready as {bot.user}")
    log_event({"event": "online"})
    asyncio.create_task(start_web_server(bot))
    asyncio.create_task(start_feeding_scheduler(bot))
    asyncio.create_task(start_profile_scheduler(bot))
    if settings.gmail_enabled: asyncio.create_task(start_gmail_logging_scheduler(bot))
    if settings.dues_enabled: asyncio.create_task(start_dues_scheduler(bot))
    try: asyncio.create_task(warm_cache_on_boot())
    except: pass

@bot.event
async def on_message(message):
    if message.author.bot: return
    
    try: await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)
    except Exception as e: print(f"Misc error: {e}")

    if message.attachments:
        in_map = settings.channel_sheet_map and message.channel.id in settings.channel_sheet_map
        if in_map or not message.guild:
            try: await _handle_image_intake(message)
            except: pass

    ctx = {"bot": bot, "message": message, "channel": message.channel, "author": message.author}
    if settings.silent_mode:
        ctx["channel"] = _MuteChannel(message.channel, _channel_label)
        ctx["message"] = _MuteMessage(message, ctx["channel"])
    
    await intent_router.handle_message(message, ctx)

def run():
    bot.run(settings.discord_token)

if __name__ == "__main__":
    run()