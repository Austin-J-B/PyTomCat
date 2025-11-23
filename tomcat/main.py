from __future__ import annotations
import asyncio
import time
import json
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

from .config import settings
from .logger import log_event, log_action
from .intent_router import IntentRouter, Intent
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

# --- PERMISSIONS LOGIC ---
async def check_permissions(user_id: int) -> dict:
    is_officer = False
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member:
            for role in member.roles:
                if role.id == settings.role_officer_id:
                    is_officer = True
                    break
        if is_officer: break
    
    return {
        "can_manage_feeding": is_officer or (user_id in settings.access_feeding_manager),
        "can_label_photos": is_officer or (user_id in settings.access_photo_labeler)
    }

# --- API & WEB SERVER ---
async def start_web_server(bot):
    app = web.Application()
    
    _CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,Authorization,X-User-ID"
    }

    async def get_index(req):
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html", headers=_CORS)
        except: return web.Response(status=404)

    async def post_auth(req):
        try:
            data = await req.json()
            # Exchange Code with Discord
            async with aiohttp.ClientSession() as sess:
                async with sess.post('https://discord.com/api/oauth2/token', data={
                    'client_id': settings.ui_activity_app_id,
                    'client_secret': settings.discord_client_secret,
                    'grant_type': 'authorization_code',
                    'code': data.get('code')
                }) as r:
                    if r.status != 200: return web.Response(status=401)
                    token = (await r.json())['access_token']
                
                async with sess.get('https://discord.com/api/users/@me', headers={'Authorization': f'Bearer {token}'}) as r:
                    user = await r.json()
            
            perms = await check_permissions(int(user['id']))
            return web.json_response({"user": user, "permissions": perms}, headers=_CORS)
        except Exception as e: return web.Response(status=500, text=str(e))

    async def get_sched(req):
        return web.json_response(load_schedule(), headers=_CORS)

    async def save_sched(req):
        try:
            uid = int(req.headers.get("X-User-ID", 0))
            perms = await check_permissions(uid)
            if not perms["can_manage_feeding"]: return web.Response(status=403)
            
            data = await req.json()
            save_schedule(data)
            return web.Response(text="Saved", headers=_CORS)
        except: return web.Response(status=500)

    async def opts(req): return web.Response(status=204, headers=_CORS)

    app.add_routes([
        web.get('/', get_index),
        web.post('/api/auth/token', post_auth),
        web.get('/api/schedule', get_sched),
        web.post('/api/schedule/save', save_sched),
        web.options('/api/auth/token', opts),
        web.options('/api/schedule/save', opts)
    ])
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8080).start()
    print("[TomCat-UI] Server on 8080")

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
    
    # 1. Always try misc handlers first (MEOW check)
    # We protect this call so errors don't block the rest
    try:
        await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)
    except Exception as e:
        print(f"Misc error: {e}")

    # 2. Image Intake
    if message.attachments:
        in_map = settings.channel_sheet_map and message.channel.id in settings.channel_sheet_map
        if in_map or not message.guild:
            try: await _handle_image_intake(message)
            except: pass

    # 3. Intent Router
    ctx = {"bot": bot, "message": message, "channel": message.channel, "author": message.author}
    if settings.silent_mode:
        # Simple mute wrapper logic would go here
        pass 
    
    await intent_router.handle_message(message, ctx)

def run():
    bot.run(settings.discord_token)

if __name__ == "__main__":
    run()