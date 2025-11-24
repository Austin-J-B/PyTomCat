"""Discord bot bootstrap: intents, startup tasks, and router wiring."""

from __future__ import annotations
import asyncio
import time
from typing import Any, Dict, Union

import os
import json
from pathlib import Path
import aiohttp
from aiohttp import web
# Import settings before first use
from .config import settings
# Config specific to your Discord App (from Developer Portal)
CLIENT_ID = getattr(settings, 'ui_activity_app_id', None) or os.getenv("UITEST_ACTIVITY_APP_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")

# Local persistence for the UI schedule
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "cache" / "feeding_schedule.json"
# Fallback station list for empty schedules (mirrors UI)
DEFAULT_STATIONS = [
    "Microwave", "Snickers", "Business", "The Greens", "HOP",
    "Lot 50", "Mary Kay & Zen", "West Hall", "Maintenance"
]

# Define your Role IDs for permissions
ROLES = {
    "FEEDING_MANAGER": 643587274797481988, # Example ID
    "PHOTO_LABELER": 798371895434149940,   # Example ID
    "VIEWER": 551082419768393729           # Example ID
}

# Your main guild ID (replace with your actual guild/server ID)
YOUR_GUILD_ID = 643586809166561310

async def auth_token_exchange(request):
    """Exchanges the temporary code from frontend for a user access token."""
    data = await request.json()
    code = data.get("code")
    # Exchange code with Discord API
    async with aiohttp.ClientSession() as session:
        async with session.post(
            'https://discord.com/api/oauth2/token',
            data={
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
            }
        ) as resp:
            if resp.status != 200:
                return web.Response(status=401, text="Invalid code")
            token_data = await resp.json()
            access_token = token_data['access_token']

        # Get User ID from Discord
        async with session.get(
            'https://discord.com/api/users/@me',
            headers={'Authorization': f'Bearer {access_token}'}
        ) as user_resp:
            user_info = await user_resp.json()
            user_id = int(user_info['id'])

    # CHECK ROLES (The important part)
    # We use your existing bot instance to check roles in the guild
    guild = bot.get_guild(YOUR_GUILD_ID)
    member = guild.get_member(user_id) if guild else None
    if not member and guild:
        # Fallback: fetch if not cached
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None

    user_roles = [r.id for r in member.roles] if member else []

    # Determine permissions
    permissions = {
        "can_edit_schedule": ROLES["FEEDING_MANAGER"] in user_roles,
        "can_label_photos": ROLES["PHOTO_LABELER"] in user_roles,
        "can_view": ROLES["VIEWER"] in user_roles
    }

    if not permissions["can_view"]:
         return web.Response(status=403, text="Not authorized to view this app.")

    # Return the token back to frontend (or a session cookie) 
    # + the permissions so the UI knows what buttons to show
    return web.json_response({
        "access_token": access_token,
        "user": user_info,
        "permissions": permissions
    })

# --- The Secure Save Endpoint ---
async def save_schedule(request):
    """Persist the feeding schedule to a local JSON file."""
    data = await request.json()
    schedule = data.get("schedule", {})
    meta = data.get("meta", {})

    # Persist to disk (cache/feeding_schedule.json)
    try:
        SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schedule": schedule,
            "meta": {**meta, "saved_at": int(time.time())}
        }
        SCHEDULE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        resp = web.Response(status=500, text=f"Failed to save schedule: {e}")
        resp.headers.update(_CORS_HEADERS)
        return resp

    resp = web.json_response({"status": "ok"})
    resp.headers.update(_CORS_HEADERS)
    return resp


async def get_schedule(request):
    """Load the last saved schedule if it exists; otherwise return empty slots."""
    if not SCHEDULE_PATH.exists():
        empty = {station: [""] * 7 for station in (getattr(settings, "feeding_schedule", {}) or {}).keys()}
        if not empty:
            empty = {station: [""] * 7 for station in DEFAULT_STATIONS}
        resp = web.json_response({"schedule": empty, "meta": {"saved_at": None}})
        resp.headers.update(_CORS_HEADERS)
        return resp

    try:
        payload = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        resp = web.json_response(payload)
        resp.headers.update(_CORS_HEADERS)
        return resp
    except Exception as e:
        resp = web.Response(status=500, text=f"Failed to load schedule: {e}")
        resp.headers.update(_CORS_HEADERS)
        return resp

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
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


async def start_web_server(bot):
    """Simple web server to serve the UI and API."""
    app = web.Application()

    async def get_index(request):
        """Serve the index.html file."""
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                resp = web.Response(text=f.read(), content_type="text/html")
                resp.headers.update(_CORS_HEADERS)
                return resp
        except FileNotFoundError:
            return web.Response(text="index.html not found. Please upload it to the bot root.", status=404)

    async def get_members(request):
        """Return JSON list of members with the Feeding Team role."""
        FEEDING_TEAM_ROLE_ID = None
        DUE_PAYING_ROLE_ID = 774442956375064606
        found_members = []
        
        for guild in bot.guilds:
            role = guild.get_role(DUE_PAYING_ROLE_ID) if DUE_PAYING_ROLE_ID else None
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

        resp = web.json_response(list(sorted_members))
        resp.headers.update(_CORS_HEADERS)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def options_members(request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    async def options_schedule(request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    async def options_schedule_save(request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    async def options_subrequest(request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    async def options_sub_open(request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    async def options_sub_claim(request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    async def submit_subrequest(request):
        """Record a manual sub request and notify the feeding team channel."""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON", headers=_CORS_HEADERS)

        user_id = data.get("user_id")
        user_name = data.get("user_name") or ""
        date_iso = data.get("date")
        stations = data.get("stations") or []
        if not user_id or not date_iso or not stations:
            return web.Response(status=400, text="Missing user_id, date, or stations", headers=_CORS_HEADERS)

        # Append to logs/subs/YYYY/YYYY-MM.jsonl
        try:
            from datetime import datetime
            from .handlers.feeding import _sub_month_key_from_date, _sub_log_path_from_key
            month_key = _sub_month_key_from_date(date_iso)
            if not month_key:
                month_key = datetime.now().strftime("%Y-%m")
            path = _sub_log_path_from_key(month_key)
            import os, json
            os.makedirs(os.path.dirname(path), exist_ok=True)
            record = {
                "kind": "sub_request",
                "id": f"sub-{int(datetime.now().timestamp()*1000)}",
                "station": ", ".join(stations),
                "stations": stations,
                "dates": [date_iso],
                "requester": int(user_id),
                "requester_name": user_name,
                "assignee": None,
                "status": "requested",
                "channel_id": 0,
                "message_id": 0,
                "created_at": datetime.now().isoformat(),
                "log_month": month_key,
                "message_preview": "",
                "trigger_phrase": "ui_sub_request",
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            return web.Response(status=500, text=f"Failed to log sub request: {e}", headers=_CORS_HEADERS)

        # Notify feeding team channel (include day-of-week and friendly date)
        try:
            channel_id = getattr(settings, "ch_feeding_team", None)
            if channel_id:
                ch = bot.get_channel(int(channel_id))
                from discord.abc import Messageable
                if isinstance(ch, Messageable):
                    # Build station list text
                    if len(stations) >= 3:
                        station_text = ", ".join(stations[:-1]) + f", and {stations[-1]}"
                    elif len(stations) == 2:
                        station_text = f"{stations[0]} and {stations[1]}"
                    else:
                        station_text = stations[0]
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(date_iso)
                        dow = dt.strftime("%A")
                        date_pretty = dt.strftime("%m/%d/%Y")
                    except Exception:
                        dow = ""
                        date_pretty = date_iso
                    msg = f"<@{user_id}> is looking for a substitute feeder for {station_text} on {dow + ', ' if dow else ''}{date_pretty}"
                    try:
                        await ch.send(msg)
                    except Exception:
                        pass
        except Exception:
            pass

        return web.json_response({"status": "ok"}, headers=_CORS_HEADERS)

    async def list_open_subs(request):
        """List sub requests separated into available, upcoming filled, and past (expired/fulfilled)."""
        import glob, json
        from datetime import datetime
        today = datetime.now().date()

        accepted_map = {}  # (parent_id, station, date_iso) -> assignee_id
        accepted_meta = {}  # (parent_id, station, date_iso) -> requester_name
        requested_items = []

        for path in glob.glob(os.path.join("logs", "subs", "*", "*.jsonl")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        status = rec.get("status")
                        stations = rec.get("stations") or ([rec.get("station")] if rec.get("station") else [])
                        dates = rec.get("dates") or []
                        date_iso = dates[0] if dates else None
                        parent_id = rec.get("id")
                        if status == "accepted":
                            assignee = rec.get("assignee")
                            for st in stations:
                                accepted_map[(parent_id, st, date_iso)] = assignee
                        elif status == "requested":
                            requester = rec.get("requester")
                            requester_name = rec.get("requester_name") or ""
                            for st in stations:
                                requested_items.append({
                                    "id": parent_id,
                                    "station": st,
                                    "date": date_iso,
                                    "requester_id": requester,
                                    "requester_name": requester_name,
                                    "assignee_id": accepted_map.get((parent_id, st, date_iso)),
                                })
            except Exception:
                continue

        available = []
        upcoming_filled = []
        past = []

        for item in requested_items:
            date_iso = item.get("date")
            try:
                d = datetime.fromisoformat(date_iso).date() if date_iso else None
            except Exception:
                d = None
            assignee = accepted_map.get((item["id"], item["station"], date_iso))
            target_list = None
            if d and d < today:
                target_list = past
            else:
                target_list = upcoming_filled if assignee else available

            out = dict(item)
            if assignee:
                out["assignee_id"] = assignee
            target_list.append(out)

        resp = web.json_response({
            "available": available,
            "upcoming_filled": upcoming_filled,
            "past": past,
        })
        resp.headers.update(_CORS_HEADERS)
        return resp

    async def claim_subs(request):
        """Mark sub requests as accepted by a user."""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON", headers=_CORS_HEADERS)
        user_id = data.get("user_id")
        picks = data.get("picks") or []
        if not user_id or not picks:
            return web.Response(status=400, text="Missing user_id or picks", headers=_CORS_HEADERS)

        from datetime import datetime
        now_iso = datetime.now().isoformat()
        messages_by_date = {}

        for pick in picks:
            parent_id = pick.get("id")
            station = pick.get("station")
            date_iso = pick.get("date")
            requester = pick.get("requester_id")
            requester_name = pick.get("requester_name") or ""
            if not parent_id or not station or not date_iso:
                continue
            try:
                # Locate log file by month
                from .handlers.feeding import _sub_month_key_from_date, _sub_log_path_from_key
                key = _sub_month_key_from_date(date_iso) or datetime.now().strftime("%Y-%m")
                path = _sub_log_path_from_key(key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                rec = {
                    "kind": "sub_accept",
                    "id": f"sub-accept-{int(datetime.now().timestamp()*1000)}",
                    "parent_id": parent_id,
                    "station": station,
                    "stations": [station],
                    "dates": [date_iso],
                    "requester": requester,
                    "assignee": int(user_id),
                    "status": "accepted",
                    "channel_id": 0,
                    "message_id": 0,
                    "created_at": now_iso,
                    "log_month": key,
                    "message_preview": "",
                    "trigger_phrase": "ui_sub_claim",
                    "requester_name": requester_name,
                }
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
                messages_by_date.setdefault(date_iso, []).append((station, requester))
            except Exception:
                continue

        # Notify feeding team channel
        try:
            channel_id = getattr(settings, "ch_feeding_team", None)
            if channel_id and messages_by_date:
                ch = bot.get_channel(int(channel_id))
                from discord.abc import Messageable
                if isinstance(ch, Messageable):
                    # Build a single aggregated message
                    try:
                        req_ids = []
                        date_bits = []
                        all_req_ids = set()
                        for date_iso, items in messages_by_date.items():
                            stations = [st for st, _ in items]
                            reqs = [req for _, req in items if req]
                            all_req_ids.update(reqs)
                            if len(stations) >= 3:
                                stations_text = ", ".join(stations[:-1]) + f", and {stations[-1]}"
                            elif len(stations) == 2:
                                stations_text = f"{stations[0]} and {stations[1]}"
                            else:
                                stations_text = stations[0]
                            try:
                                dt = datetime.fromisoformat(date_iso)
                                dow = dt.strftime("%A")
                                date_pretty = dt.strftime("%m/%d/%Y")
                            except Exception:
                                dow = ""
                                date_pretty = date_iso
                            date_bits.append(f"{stations_text} on {dow + ', ' if dow else ''}{date_pretty}")
                        req_mentions = ""
                        if len(all_req_ids) >= 2:
                            req_mentions = " and ".join([f"<@{rid}>" for rid in all_req_ids])
                        elif len(all_req_ids) == 1:
                            req_mentions = f"<@{list(all_req_ids)[0]}>"
                        else:
                            req_mentions = "someone"
                        msg = f"<@{user_id}> picked up {req_mentions}'s substitute request for " + " and ".join(date_bits)
                        await ch.send(msg)
                    except Exception:
                        pass
        except Exception:
            pass

        return web.json_response({"status": "ok"}, headers=_CORS_HEADERS)

    app.add_routes([
        web.get('/', get_index),
        web.get('/api/members', get_members),
        web.options('/api/members', options_members),
        web.post('/api/auth/token', auth_token_exchange),
        web.post('/api/schedule/save', save_schedule),
        web.get('/api/schedule', get_schedule),
        web.options('/api/schedule', options_schedule),
        web.options('/api/schedule/save', options_schedule_save),
        web.post('/api/subrequest', submit_subrequest),
        web.options('/api/subrequest', options_subrequest),
        web.get('/api/subs/open', list_open_subs),
        web.options('/api/subs/open', options_sub_open),
        web.post('/api/subs/claim', claim_subs),
        web.options('/api/subs/claim', options_sub_claim),
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
