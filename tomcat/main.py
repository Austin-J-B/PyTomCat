"""Discord bot bootstrap: intents, startup tasks, and router wiring."""

from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import time
from typing import Any, Dict, Optional, Union

import os
import json
from pathlib import Path
import aiohttp
from aiohttp import web
import logging
# Import settings before first use
from .config import settings
# Config specific to your Discord App (from Developer Portal)
CLIENT_ID = getattr(settings, 'ui_activity_app_id', None) or os.getenv("UITEST_ACTIVITY_APP_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")

SESSION_SECRET = os.getenv("UI_SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("UI_SESSION_SECRET is required to issue UI session cookies")

SESSION_TTL_SECONDS = int(os.getenv("UI_SESSION_TTL_SECONDS", "3600"))
# Default secure cookies ON so cross-site (e.g., github pages -> your domain) can send them.
_COOKIE_SECURE = os.getenv("UI_COOKIE_SECURE", "true").lower() == "true"
# If secure, use SameSite=None (required for third-party); otherwise keep Lax for localhost testing.
_COOKIE_SAMESITE = "None" if _COOKIE_SECURE else "Lax"

_ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv("UI_ALLOWED_ORIGINS", "http://localhost:8080").split(",")
    if origin.strip()
}

_AUTH_DEBUG = os.getenv("UI_AUTH_DEBUG", "false").lower() == "true"

# Officer role configured for elevated permissions (edit schedule, impersonation)
OFFICER_ROLE_ID = 845035667661783061


def _debug(msg: str) -> None:
    """Print lightweight auth/debug traces when enabled."""
    if _AUTH_DEBUG:
        print(f"[UI-AUTH] {msg}")

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
# UI can override via env if needed
UI_GUILD_ID = int(os.getenv("UI_GUILD_ID", str(YOUR_GUILD_ID or 0)) or 0)


def _b64_encode(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode("utf-8")).decode("utf-8").rstrip("=")


def _b64_decode(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8")


def _sign_payload(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_b64_encode(body)}.{sig}"


def _verify_session(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    try:
        encoded_body, sig = token.split(".", 1)
        body = _b64_decode(encoded_body)
        expected_sig = hmac.new(
            SESSION_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, sig):
            return None
        payload = json.loads(body)
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _get_session_from_request(request: web.Request) -> Optional[dict]:
    token = request.cookies.get("tc_session")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1]
    _debug(f"session cookie_present={bool(token)}")
    return _verify_session(token) if token else None


def _issue_session_response(user_info: dict, permissions: dict, request: web.Request) -> web.Response:
    """Sign a session payload and return a JSON response with cookie set."""
    now_ts = int(time.time())
    session_payload = {
        "user_id": str(user_info.get("id")),
        "username": user_info.get("username"),
        "global_name": user_info.get("global_name"),
        "permissions": permissions,
        "iat": now_ts,
        "exp": now_ts + SESSION_TTL_SECONDS,
    }
    session_token = _sign_payload(session_payload)

    resp = web.json_response({
        "user": {
            "id": user_info.get("id"),
            "username": user_info.get("username"),
            "global_name": user_info.get("global_name"),
        },
        "permissions": permissions
    })
    resp.set_cookie(
        "tc_session",
        session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite=_COOKIE_SAMESITE,
    )
    _debug(f"set-cookie secure={_COOKIE_SECURE} samesite={_COOKIE_SAMESITE}")
    return _with_cors(resp, request)


async def _resolve_member(user_id: int) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
    """Find a member in the configured guild or any guild the bot is in."""
    guild = bot.get_guild(YOUR_GUILD_ID) if "bot" in globals() else None
    member = guild.get_member(user_id) if guild else None
    if not member and guild:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None

    if not member and "bot" in globals():
        for g in bot.guilds:
            try:
                candidate = g.get_member(user_id)
                if not candidate:
                    candidate = await g.fetch_member(user_id)
                if candidate:
                    return g, candidate
            except Exception:
                continue
    return guild, member


def _build_permissions(user_roles: list[int]) -> dict:
    """Calculate permissions based on Discord roles."""
    is_officer = OFFICER_ROLE_ID in user_roles
    return {
        "can_edit_schedule": is_officer,
        "can_label_photos": ROLES.get("PHOTO_LABELER") in user_roles,
        "can_view": True,
        "is_officer": is_officer,
    }


def _require_permissions(
    request: web.Request,
    *,
    require_view: bool = False,
    require_edit: bool = False,
) -> tuple[Optional[dict], Optional[web.Response]]:
    session = _get_session_from_request(request)
    if not session:
        _debug(f"_require_permissions missing session require_view={require_view} require_edit={require_edit}")
        return None, _with_cors(web.Response(status=401, text="Missing or invalid session"), request)

    permissions = session.get("permissions", {})
    if require_view and not permissions.get("can_view"):
        return None, _with_cors(web.Response(status=403, text="Not authorized to view"), request)
    if require_edit and not permissions.get("can_edit_schedule"):
        return None, _with_cors(web.Response(status=403, text="Schedule editing not allowed"), request)
    return session, None

async def auth_token_exchange(request):
    """Exchanges the temporary code from frontend for a user access token."""
    _debug(f"POST /api/auth/token headers_origin={request.headers.get('Origin')} query={dict(request.query)}")
    try:
        data = await request.json()
    except Exception:
        return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

    code = data.get("code")
    # 1. Capture the redirect_uri sent from the frontend
    redirect_uri = data.get("redirect_uri") 
    _debug(f"payload redirect_uri={redirect_uri} code_present={bool(code)}")

    if not code:
        return _with_cors(web.Response(status=400, text="Missing authorization code"), request)
    if not CLIENT_SECRET or not CLIENT_ID:
        return _with_cors(web.Response(status=500, text="OAuth client is not configured"), request)

    # Exchange code with Discord API
    async with aiohttp.ClientSession() as session:
        token_payload = {
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
        }
        
        # 2. CRITICAL FIX: Pass the redirect_uri to Discord if it exists
        if redirect_uri:
            token_payload['redirect_uri'] = redirect_uri

        try:
            async with session.post(
                'https://discord.com/api/oauth2/token',
                data=token_payload,
            ) as resp:
                if resp.status != 200:
                    # Log the exact error from Discord to your console
                    err_text = await resp.text()
                    _debug(f"discord token error status={resp.status} body={err_text}")
                    return _with_cors(
                        web.Response(
                            status=401,
                            text=f"Token exchange failed (discord status={resp.status})"
                        ),
                        request
                    )
            
                token_data = await resp.json()
                access_token = token_data.get('access_token')
                _debug(f"discord token ok scopes={token_data.get('scope')} expires_in={token_data.get('expires_in')}")
        except Exception as exc:
            _debug(f"discord token exception={exc}")
            return _with_cors(web.Response(status=502, text="Token exchange request failed"), request)

        # Get User ID from Discord
        try:
            async with session.get(
                'https://discord.com/api/users/@me',
                headers={'Authorization': f'Bearer {access_token}'}
            ) as user_resp:
                user_info = await user_resp.json()
                user_id = int(user_info['id'])
                _debug(f"discord /users/@me status={user_resp.status} user_id={user_id}")
        except Exception as exc:
            _debug(f"/users/@me exception={exc}")
            return _with_cors(web.Response(status=502, text="Failed to fetch user profile"), request)

    # CHECK ROLES
    guild, member = await _resolve_member(user_id)
    if not guild or not member:
        guild_ids = [getattr(g, "id", None) for g in bot.guilds]
        _debug(f"guild/member missing guild={bool(guild)} member={bool(member)} bot_guilds={guild_ids}")
        return _with_cors(web.Response(status=403, text="Unable to validate guild membership"), request)

    user_roles = [r.id for r in member.roles] if member else []
    permissions = _build_permissions(user_roles)

    return _issue_session_response(user_info, permissions, request)


async def get_session(request: web.Request):
    """Validate an existing session cookie and refresh it."""
    session = _get_session_from_request(request)
    if not session:
        return _with_cors(web.Response(status=401, text="Missing or invalid session"), request)

    try:
        user_id = int(session.get("user_id"))
    except Exception:
        return _with_cors(web.Response(status=400, text="Invalid session payload"), request)

    guild, member = await _resolve_member(user_id)
    if not guild or not member:
        return _with_cors(web.Response(status=403, text="Guild membership required"), request)

    user_roles = [r.id for r in member.roles]
    permissions = _build_permissions(user_roles)
    user_info = {
        "id": user_id,
        "username": session.get("username") or getattr(member, "name", ""),
        "global_name": session.get("global_name") or getattr(member, "global_name", None),
    }

    return _issue_session_response(user_info, permissions, request)

# --- The Secure Save Endpoint ---
async def save_schedule(request):
    """Persist the feeding schedule to a local JSON file."""
    _, error = _require_permissions(request, require_view=True, require_edit=True)
    if error:
        return error
    try:
        data = await request.json()
    except Exception:
        return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

    schedule = data.get("schedule", {})
    meta = data.get("meta", {})
    if not isinstance(schedule, dict):
        return _with_cors(web.Response(status=400, text="Schedule must be an object"), request)

    # Persist to disk (cache/feeding_schedule.json)
    try:
        SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schedule": schedule,
            "meta": {**meta, "saved_at": int(time.time())}
        }
        SCHEDULE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        logging.exception("Unexpected error when saving schedule")
        return _with_cors(web.Response(status=500, text="Failed to save schedule."), request)

    return _with_cors(web.json_response({"status": "ok"}), request)


async def get_schedule(request):
    """Load the last saved schedule if it exists; otherwise return empty slots."""
    _, error = _require_permissions(request, require_view=True)
    if error:
        return error
    _debug(f"GET /api/schedule session_ok")
    if not SCHEDULE_PATH.exists():
        empty = {station: [""] * 7 for station in (getattr(settings, "feeding_schedule", {}) or {}).keys()}
        if not empty:
            empty = {station: [""] * 7 for station in DEFAULT_STATIONS}
        return _with_cors(web.json_response({"schedule": empty, "meta": {"saved_at": None}}), request)

    try:
        payload = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        sched = payload.get("schedule", {}) or {}
        _debug(f"/api/schedule returning stations={list(sched.keys())} saved_at={payload.get('meta',{}).get('saved_at')}")
        return _with_cors(web.json_response(payload), request)
    except Exception as e:
        return _with_cors(web.Response(status=500, text=f"Failed to load schedule: {e}"), request)

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

def _cors_headers(request: web.Request) -> Dict[str, str]:
    origin = request.headers.get("Origin")
    allow_origin = None
    if origin and ("*" in _ALLOWED_ORIGINS or origin in _ALLOWED_ORIGINS):
        allow_origin = origin
    elif "*" in _ALLOWED_ORIGINS:
        allow_origin = "*"

    headers = {
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Vary": "Origin",
    }
    _debug(f"CORS origin={origin!r} allow_origin={allow_origin!r}")
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin
    if allow_origin and allow_origin != "*":
        headers["Access-Control-Allow-Credentials"] = "true"
    return headers


def _with_cors(resp: web.StreamResponse, request: web.Request) -> web.StreamResponse:
    resp.headers.update(_cors_headers(request))
    return resp


async def start_web_server(bot):
    """Simple web server to serve the UI and API."""
    app = web.Application()

    async def get_index(request):
        """Serve the index.html file."""
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                return _with_cors(web.Response(text=f.read(), content_type="text/html"), request)
        except FileNotFoundError:
            return web.Response(text="index.html not found. Please upload it to the bot root.", status=404)

    async def get_members(request):
        """Return JSON list of members allowed to be scheduled."""
        _, error = _require_permissions(request, require_view=True)
        if error:
            return error
        FEEDING_TEAM_ROLE_ID = None
        DUE_PAYING_ROLE_ID = 774442956375064606
        HOLIDAY_FEEDER_ROLE_ID = 1419369282634125384
        found_members = []
        
        for guild in bot.guilds:
            for role_id in (DUE_PAYING_ROLE_ID, HOLIDAY_FEEDER_ROLE_ID):
                role = guild.get_role(role_id) if role_id else None
                if not role:
                    continue
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

        _debug(f"/api/members count={len(sorted_members)} roles={[DUE_PAYING_ROLE_ID, HOLIDAY_FEEDER_ROLE_ID]}")

        resp = web.json_response(list(sorted_members))
        resp.headers["Cache-Control"] = "no-store"
        return _with_cors(resp, request)

    async def options_members(request):
        return _with_cors(web.Response(status=204), request)

    async def options_schedule(request):
        return _with_cors(web.Response(status=204), request)

    async def options_schedule_save(request):
        return _with_cors(web.Response(status=204), request)

    async def options_auth_token(request):
        return _with_cors(web.Response(status=204), request)

    async def options_auth_session(request):
        return _with_cors(web.Response(status=204), request)

    async def options_subrequest(request):
        return _with_cors(web.Response(status=204), request)

    async def options_sub_open(request):
        return _with_cors(web.Response(status=204), request)

    async def options_sub_claim(request):
        return _with_cors(web.Response(status=204), request)

    async def submit_subrequest(request):
        """Record a manual sub request."""
        session, error = _require_permissions(request, require_view=True)
        if error: return error
        
        try:
            data = await request.json()
        except Exception:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

        # --- SECURITY / IMPERSONATION LOGIC ---
        req_user_id = data.get("user_id")
        req_user_name = data.get("user_name")
        
        # If user is NOT an officer, force them to use their own identity
        permissions = session.get("permissions", {})
        if not permissions.get("is_officer"):
            req_user_id = session.get("user_id")
            req_user_name = session.get("username") # fallback
        
        # If officer didn't provide a specific user (standard submit), default to self
        if not req_user_id:
            req_user_id = session.get("user_id")

        date_iso = data.get("date")
        stations = data.get("stations") or []
        
        if not req_user_id or not date_iso or not stations:
            return _with_cors(web.Response(status=400, text="Missing required fields"), request)

        # Append to logs/subs/YYYY/YYYY-MM.jsonl
        try:
            from datetime import datetime
            from .handlers.feeding import _sub_month_key_from_date, _sub_log_path_from_key
            month_key = _sub_month_key_from_date(date_iso) or datetime.now().strftime("%Y-%m")
            path = _sub_log_path_from_key(month_key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            
            record = {
                "kind": "sub_request",
                "id": f"sub-{int(datetime.now().timestamp()*1000)}",
                "station": ", ".join(stations),
                "stations": stations,
                "dates": [date_iso],
                "requester": int(req_user_id),
                "requester_name": req_user_name,
                "assignee": None,
                "status": "requested",
                "created_at": datetime.now().isoformat(),
                "trigger_phrase": "ui_sub_request",
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            return _with_cors(web.Response(status=500, text=f"Log error: {e}"), request)

        # Notify Discord
        try:
            channel_id = getattr(settings, "ch_feeding_team", None)
            if channel_id:
                ch = bot.get_channel(int(channel_id))
                if hasattr(ch, 'send'):
                    stations_str = ", ".join(stations)
                    msg = f"<@{req_user_id}> requested a sub for **{stations_str}** on {date_iso}."
                    await ch.send(msg)
        except Exception:
            pass

        return _with_cors(web.json_response({"status": "ok"}), request)
    
    async def delete_subrequest(request):
        """Physically remove a request from the log file."""
        session, error = _require_permissions(request, require_view=True)
        if error: return error

        try:
            data = await request.json()
        except:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

        target_id = data.get("id")
        date_iso = data.get("date")
        
        if not target_id or not date_iso:
            return _with_cors(web.Response(status=400, text="Missing ID or Date"), request)

        from .handlers.feeding import _sub_month_key_from_date, _sub_log_path_from_key
        month_key = _sub_month_key_from_date(date_iso)
        path = _sub_log_path_from_key(month_key)

        if not os.path.exists(path):
            return _with_cors(web.Response(status=404, text="Record not found"), request)

        # Re-write file excluding the item
        new_lines = []
        deleted_item = None
        user_id_str = str(session.get("user_id"))
        is_officer = session.get("permissions", {}).get("is_officer")

        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("id") == target_id:
                            # Permission Check
                            requester_id = str(rec.get("requester") or "")
                            assignee_id = str(rec.get("assignee") or "")
                            is_owner = user_id_str in (requester_id, assignee_id)
                            if not is_officer and not is_owner:
                                return _with_cors(web.Response(status=403, text="You can only delete your own items."), request)
                            deleted_item = rec
                            continue # Skip this line (Delete)
                        new_lines.append(line)
                    except:
                        new_lines.append(line)
            
            if deleted_item:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                
                # Notify Discord
                ch_id = getattr(settings, "ch_feeding_team", None)
                if ch_id:
                    ch = bot.get_channel(int(ch_id))
                    if hasattr(ch, 'send'):
                        actor_name = session.get("username", "Unknown")
                        kind = "Request" if deleted_item.get("kind") == "sub_request" else "Claim"
                        st = deleted_item.get("station") or "Unknown"
                        await ch.send(f"🗑️ **{kind} Deleted**: {actor_name} removed the item for **{st}** on {date_iso}.")

                return _with_cors(web.json_response({"status": "ok"}), request)
            else:
                return _with_cors(web.Response(status=404, text="Item ID not found in log"), request)

        except Exception as e:
            return _with_cors(web.Response(status=500, text=str(e)), request)
    

    
    async def list_open_subs(request):
        """List sub requests separated into available, upcoming filled, and past (expired/fulfilled)."""
        _, error = _require_permissions(request, require_view=True)
        if error:
            return error
        import glob, json
        from datetime import datetime
        today = datetime.now().date()

        accepted_map = {}  # (parent_id, station, date_iso) -> assignee_id
        accepted_meta = {}  # (parent_id, station, date_iso) -> requester_name
        requested_items = []
        missing_requester_ids = set()
        missing_assignee_ids = set()

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
                            if assignee:
                                try:
                                    missing_assignee_ids.add(int(assignee))
                                except Exception:
                                    pass
                            for st in stations:
                                accepted_map[(rec.get("parent_id") or parent_id, st, date_iso)] = assignee
                        elif status == "requested":
                            requester = rec.get("requester")
                            requester_name = rec.get("requester_name") or ""
                            if requester and not requester_name:
                                try:
                                    missing_requester_ids.add(int(requester))
                                except Exception:
                                    pass
                            for st in stations:
                                requested_items.append({
                                    "id": parent_id,
                                    "station": st,
                                    "date": date_iso,
                                    "requester_id": requester,
                                    "requester_name": requester_name,
                                    "assignee_id": None,
                                    "assignee_name": rec.get("assignee_name") or "",
                                })
            except Exception:
                continue

        # Resolve display names for any requester IDs that were missing names in the log.
        name_cache: Dict[int, str] = {}
        if missing_requester_ids:
            guild = bot.get_guild(YOUR_GUILD_ID) if "bot" in globals() else None
            for uid in list(missing_requester_ids):
                display = ""
                try:
                    if guild:
                        member = guild.get_member(uid)
                        if not member:
                            try:
                                member = await guild.fetch_member(uid)
                            except Exception:
                                member = None
                        if member:
                            display = getattr(member, "display_name", None) or getattr(member, "global_name", None) or member.name
                    if not display and "bot" in globals():
                        user = bot.get_user(uid)  # type: ignore[name-defined]
                        if not user and hasattr(bot, "fetch_user"):
                            try:
                                user = await bot.fetch_user(uid)  # type: ignore[attr-defined]
                            except Exception:
                                user = None
                        if user:
                            display = getattr(user, "global_name", None) or getattr(user, "name", None) or ""
                except Exception:
                    display = ""
                if display:
                    name_cache[uid] = display

        assignee_name_cache: Dict[int, str] = {}
        if missing_assignee_ids:
            guild = bot.get_guild(YOUR_GUILD_ID) if "bot" in globals() else None
            for uid in list(missing_assignee_ids):
                display = ""
                try:
                    if guild:
                        member = guild.get_member(uid)
                        if not member:
                            try:
                                member = await guild.fetch_member(uid)
                            except Exception:
                                member = None
                        if member:
                            display = getattr(member, "display_name", None) or getattr(member, "global_name", None) or member.name
                    if not display and "bot" in globals():
                        user = bot.get_user(uid)  # type: ignore[name-defined]
                        if not user and hasattr(bot, "fetch_user"):
                            try:
                                user = await bot.fetch_user(uid)  # type: ignore[attr-defined]
                            except Exception:
                                user = None
                        if user:
                            display = getattr(user, "global_name", None) or getattr(user, "name", None) or ""
                except Exception:
                    display = ""
                if display:
                    assignee_name_cache[uid] = display

        # After reading all records, populate requester/assignee names and assignee ids from the accept map
        for item in requested_items:
            key = (item.get("id"), item.get("station"), item.get("date"))
            assignee = accepted_map.get(key)
            if assignee:
                item["assignee_id"] = assignee
                if not item.get("assignee_name"):
                    try:
                        item["assignee_name"] = assignee_name_cache.get(int(assignee), "")
                    except Exception:
                        item["assignee_name"] = ""
            if item.get("requester_id") and not item.get("requester_name"):
                try:
                    item["requester_name"] = name_cache.get(int(item["requester_id"]), "")
                except Exception:
                    item["requester_name"] = ""

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
            requester_id = item.get("requester_id")
            if requester_id and not out.get("requester_name"):
                try:
                    out["requester_name"] = name_cache.get(int(requester_id), "")
                except Exception:
                    out["requester_name"] = ""
            if assignee:
                out["assignee_id"] = str(assignee)
                if not out.get("assignee_name"):
                    try:
                        out["assignee_name"] = assignee_name_cache.get(int(assignee), "")
                    except Exception:
                        out["assignee_name"] = ""
            if requester_id:
                out["requester_id"] = str(requester_id)
            target_list.append(out)

        return _with_cors(web.json_response({
            "available": available,
            "upcoming_filled": upcoming_filled,
            "past": past,
        }), request)

    async def claim_subs(request):
        """Mark sub requests as accepted by a user."""
        session, error = _require_permissions(request, require_view=True)
        if error:
            return error
        try:
            data = await request.json()
        except Exception:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)
        user_id = data.get("user_id") or session.get("user_id")
        picks = data.get("picks") or []
        if not user_id or not picks:
            return _with_cors(web.Response(status=400, text="Missing user_id or picks"), request)

        from datetime import datetime
        now_iso = datetime.now().isoformat()
        messages_by_date = {}  # date_iso -> list of (station, requester_id, requester_name)

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
                messages_by_date.setdefault(date_iso, []).append((station, requester, requester_name))
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
                        req_mentions_set = set()
                        date_bits = []
                        for date_iso, items in messages_by_date.items():
                            stations = [st for st, _, _ in items]
                            reqs = [(req, req_name) for _, req, req_name in items]
                            for req, req_name in reqs:
                                mention = None
                                if req and str(req).isdigit():
                                    mention = f"<@{req}>"
                                elif req_name:
                                    mention = req_name
                                else:
                                    mention = "someone"
                                req_mentions_set.add(mention)
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
                        if len(req_mentions_set) >= 2:
                            req_mentions = " and ".join(req_mentions_set)
                        elif len(req_mentions_set) == 1:
                            req_mentions = next(iter(req_mentions_set))
                        else:
                            req_mentions = "someone"
                        msg = f"<@{user_id}> picked up {req_mentions}'s substitute request for " + " and ".join(date_bits)
                        await ch.send(msg)
                    except Exception:
                        pass
        except Exception:
            pass

        return _with_cors(web.json_response({"status": "ok"}), request)

    app.add_routes([
        web.get('/', get_index),
        web.get('/api/members', get_members),
        web.options('/api/members', options_members),
        web.post('/api/auth/token', auth_token_exchange),
        web.get('/api/auth/session', get_session),
        web.options('/api/auth/token', options_auth_token),
        web.options('/api/auth/session', options_auth_session),
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
        web.post('/api/subrequest/delete', delete_subrequest),
        web.options('/api/subrequest/delete', options_subrequest), 
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    # Listen on localhost to avoid exposing the UI externally by default
    site = web.TCPSite(runner, '127.0.0.1', 8080)
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
