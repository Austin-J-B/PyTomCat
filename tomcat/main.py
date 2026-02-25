"""Discord bot bootstrap: intents, startup tasks, and router wiring."""

from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import time
import secrets
from typing import Any, Callable, Dict, Optional, Union
from collections import deque
from datetime import datetime

import os
import json
from pathlib import Path
import aiohttp
from aiohttp import web
import logging
#Import settings before first use
from .config import settings
#Config specific to your Discord App (from Developer Portal)
CLIENT_ID = getattr(settings, 'ui_activity_app_id', None) or os.getenv("UITEST_ACTIVITY_APP_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")

SESSION_SECRET = os.getenv("UI_SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("UI_SESSION_SECRET is required to issue UI session cookies (set a long random value in .env)")

SESSION_TTL_SECONDS = int(os.getenv("UI_SESSION_TTL_SECONDS", "3600"))
#Default secure cookies ON so cross-site (e.g., github pages -> your domain) can send them.
_COOKIE_SECURE = os.getenv("UI_COOKIE_SECURE", "true").lower() == "true"
#If secure, use SameSite=None (required for third-party); otherwise keep Lax for localhost testing.
_COOKIE_SAMESITE = "None" if _COOKIE_SECURE else "Lax"

_ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv("UI_ALLOWED_ORIGINS", "http://localhost:8080").split(",")
    if origin.strip()
}

_AUTH_DEBUG = os.getenv("UI_AUTH_DEBUG", "false").lower() == "true"

#Officer/guild/role IDs are configured via settings/env only (no code defaults).
OFFICER_ROLE_IDS: list[int] = []
for _rid in (getattr(settings, "officer_role_ids", []) or []):
    _rid = int(_rid or 0)
    if _rid and _rid not in OFFICER_ROLE_IDS:
        OFFICER_ROLE_IDS.append(_rid)
_OFFICER_ROLE_ID_FALLBACK = int(getattr(settings, "officer_role_id", 0) or 0)
if _OFFICER_ROLE_ID_FALLBACK and _OFFICER_ROLE_ID_FALLBACK not in OFFICER_ROLE_IDS:
    OFFICER_ROLE_IDS.append(_OFFICER_ROLE_ID_FALLBACK)
OFFICER_ROLE_IDS_SET = set(OFFICER_ROLE_IDS)
OFFICER_ROLE_ID = OFFICER_ROLE_IDS[0] if OFFICER_ROLE_IDS else 0


def _debug(msg: str) -> None:
    """Print lightweight auth/debug traces when enabled."""
    if _AUTH_DEBUG:
        print(f"[UI-AUTH] {msg}")

#Local persistence for the UI schedule (ndjson), with legacy JSON fallback for migration
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "cache" / "feeding_schedule.ndjson"
LEGACY_SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "cache" / "feeding_schedule.json"
#Versioned schedule helpers
_DEFAULT_SCHED_EFFECTIVE = "1970-01-01"

def _week_start_iso(dt: datetime | None = None) -> str:
    d = (dt or datetime.now()).date()
    days_to_sunday = (d.weekday() + 1) % 7  #Monday=0 ->1 day back; Sunday=6 ->0
    sunday = d - timedelta(days=days_to_sunday)
    return sunday.isoformat()
#Rate limiting constants for the web API
RATE_LIMIT_MAX_REQUESTS = 200
RATE_LIMIT_WINDOW_SECONDS = 60
LABELER_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("LABELER_RATE_LIMIT_MAX_REQUESTS", "700") or "700")
LABELER_IMAGE_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("LABELER_IMAGE_RATE_LIMIT_MAX_REQUESTS", "1800") or "1800")
_rate_limit_counters: dict[str, deque] = {}
_background_tasks: dict[str, asyncio.Task] = {}


def _start_background_task(name: str, coro_factory: Callable[[], Any]) -> None:
    """Start a named background task once; restart only if previous finished/failed."""
    existing = _background_tasks.get(name)
    if existing and not existing.done():
        return
    _background_tasks[name] = asyncio.create_task(coro_factory())

#Define your Role IDs for permissions
ROLES = {
    "FEEDING_MANAGER": int(getattr(settings, "role_feeding_manager_id", 0) or 0),
    "PHOTO_LABELER": int(getattr(settings, "role_photo_labeler_id", 0) or 0),
    "VIEWER": int(getattr(settings, "role_viewer_id", 0) or 0),
}

#Your main guild ID (replace with your actual guild/server ID)
YOUR_GUILD_ID = int(getattr(settings, "ui_guild_id", None) or getattr(settings, "target_guild_id", None) or 0)
#UI can override via env if needed
UI_GUILD_ID = YOUR_GUILD_ID


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
    csrf_token = secrets.token_urlsafe(32)
    #CRITICAL FIX: Ensure user_id is a string to prevent JS integer precision loss
    user_id_str = str(user_info.get("id"))
    
    session_payload = {
        "user_id": user_id_str, 
        "username": user_info.get("username"),
        "global_name": user_info.get("global_name"),
        "permissions": permissions,
        "csrf": csrf_token,
        "iat": now_ts,
        "exp": now_ts + SESSION_TTL_SECONDS,
    }
    session_token = _sign_payload(session_payload)

    resp = web.json_response({
        "user": {
            "id": user_id_str,
            "username": user_info.get("username"),
            "global_name": user_info.get("global_name"),
        },
        "permissions": permissions,
        "csrf_token": csrf_token,
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


#--- schedule version helpers ---
def _read_schedule_ndjson(path: Path) -> list:
    versions: list = []
    if not path.exists():
        return versions
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("effective_from"):
                versions.append({
                    "effective_from": obj.get("effective_from"),
                    "schedule": obj.get("schedule") or {},
                    "meta": obj.get("meta") or {}
                })
    except Exception:
        return versions
    return versions


def _load_schedule_versions() -> list:
    versions = _read_schedule_ndjson(SCHEDULE_PATH)
    if versions:
        return versions

    #Legacy JSON fallback (migrates forward to ndjson)
    if not LEGACY_SCHEDULE_PATH.exists():
        return []
    try:
        data = json.loads(LEGACY_SCHEDULE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "versions" in data:
            versions = data.get("versions") or []
        elif isinstance(data, dict) and "schedule" in data:
            versions = [{"effective_from": _DEFAULT_SCHED_EFFECTIVE, "schedule": data.get("schedule") or {}, "meta": data.get("meta") or {}}]
        elif isinstance(data, list):
            versions = data
        if versions:
            _save_schedule_versions(versions)
        return versions
    except Exception:
        return []
    return []


def _save_schedule_versions(versions: list):
    meta = {"updated_at": int(time.time())}
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_PATH.with_name(SCHEDULE_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for v in versions:
            f.write(json.dumps(v, separators=(",", ":")) + "\n")
        f.write(json.dumps({"meta": meta}, separators=(",", ":")) + "\n")
    tmp.replace(SCHEDULE_PATH)


def _resolve_schedule_for_date(target_iso: Optional[str]) -> dict:
    """Return schedule and effective_from for the given date (YYYY-MM-DD)."""
    versions = _load_schedule_versions()
    if not versions:
        return {"schedule": {}, "effective_from": _DEFAULT_SCHED_EFFECTIVE, "meta": {}}
    target_date = None
    if target_iso:
        try:
            target_date = datetime.fromisoformat(target_iso).date()
        except Exception:
            target_date = None
    if not target_date:
        target_date = datetime.now().date()

    best = None
    for v in versions:
        try:
            eff = datetime.fromisoformat(str(v.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)).date()
        except Exception:
            continue
        if eff <= target_date and (best is None or datetime.fromisoformat(str(best.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)).date() < eff):
            best = v
    if not best:
        best = sorted(versions, key=lambda x: x.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)[0]

    sched = best.get("schedule") or {}
    #Filter schedule to known station names for that effective date
    allowed = set(station_names(best.get("effective_from")))
    sched = {st: row for st, row in sched.items() if st in allowed}
    return {"schedule": sched, "effective_from": best.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE, "meta": best.get("meta") or {}}


def _upsert_schedule_version(schedule: dict, effective_from: str, meta: dict | None = None) -> list:
    try:
        eff = datetime.fromisoformat(str(effective_from)).date().isoformat()
    except Exception:
        eff = _DEFAULT_SCHED_EFFECTIVE
    versions = _load_schedule_versions()
    replaced = False
    for v in versions:
        if str(v.get("effective_from")) == eff:
            v["schedule"] = schedule
            v["meta"] = meta or v.get("meta") or {}
            replaced = True
            break
    if not replaced:
        versions.append({"effective_from": eff, "schedule": schedule, "meta": meta or {}})
    versions = sorted(versions, key=lambda x: x.get("effective_from") or _DEFAULT_SCHED_EFFECTIVE)
    _save_schedule_versions(versions)
    return versions


async def _resolve_member_once(
    user_id: int,
) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
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


async def _resolve_member(user_id: int) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
    """Find a member with a short retry to smooth Discord cache/API lag."""
    guild: Optional[discord.Guild] = None
    member: Optional[discord.Member] = None
    for attempt in range(3):
        if "bot" in globals():
            try:
                if hasattr(bot, "is_ready") and not bot.is_ready():
                    await bot.wait_until_ready()
            except Exception:
                pass
        guild, member = await _resolve_member_once(user_id)
        if member:
            return guild, member
        if attempt < 2:
            await asyncio.sleep(0.4 * (attempt + 1))
    return guild, member


def _build_permissions(user_roles: list[int]) -> dict:
    """Calculate permissions based on Discord roles."""
    is_officer = any(int(role_id or 0) in OFFICER_ROLE_IDS_SET for role_id in user_roles)
    photo_labeler_role = int(ROLES.get("PHOTO_LABELER") or getattr(settings, "role_photo_labeler", 0) or 0)
    can_label_photos = is_officer or (photo_labeler_role in user_roles if photo_labeler_role else False)
    return {
        "can_edit_schedule": is_officer,
        "can_label_photos": can_label_photos,
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


def _require_csrf(request: web.Request, session: dict) -> Optional[web.Response]:
    """Require CSRF token for state-changing requests."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    expected = session.get("csrf")
    provided = request.headers.get("X-CSRF-Token") or request.headers.get("X-TC-CSRF")
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        return _with_cors(web.Response(status=403, text="Missing or invalid CSRF token"), request)
    return None

async def auth_token_exchange(request):
    """Exchanges the temporary code from frontend for a user access token."""
    _debug(f"POST /api/auth/token headers_origin={request.headers.get('Origin')} query={dict(request.query)}")
    try:
        data = await request.json()
    except Exception:
        return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

    code = data.get("code")
    redirect_uri = data.get("redirect_uri") 
    _debug(f"payload redirect_uri={redirect_uri} code_present={bool(code)}")

    if not code:
        return _with_cors(web.Response(status=400, text="Missing authorization code"), request)
    if not CLIENT_SECRET or not CLIENT_ID:
        return _with_cors(web.Response(status=500, text="OAuth client is not configured"), request)

    async with aiohttp.ClientSession() as session:
        token_payload = {
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
        }
        
        if redirect_uri:
            token_payload['redirect_uri'] = redirect_uri

        try:
            async with session.post(
                'https://discord.com/api/oauth2/token',
                data=token_payload,
            ) as resp:
                if resp.status != 200:
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

#--- The Secure Save Endpoint ---
async def save_schedule(request):
    """Persist the feeding schedule to a local JSON file."""
    session, error = _require_permissions(request, require_view=True, require_edit=True)
    if error:
        return error
    csrf_error = _require_csrf(request, session)
    if csrf_error:
        return csrf_error
    try:
        data = await request.json()
    except Exception:
        return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

    schedule = data.get("schedule", {})
    meta = data.get("meta", {})
    effective_from = data.get("effective_from") or data.get("week")
    if not isinstance(schedule, dict):
        return _with_cors(web.Response(status=400, text="Schedule must be an object"), request)
    if not effective_from:
        return _with_cors(web.Response(status=400, text="effective_from is required"), request)

    try:
        meta_out = {**meta, "saved_at": int(time.time())}
        _upsert_schedule_version(schedule, effective_from, meta_out)
    except Exception:
        logging.exception("Unexpected error when saving schedule")
        return _with_cors(web.Response(status=500, text="Failed to save schedule."), request)

    return _with_cors(web.json_response({"status": "ok"}), request)


async def get_schedule(request):
    """Load the last saved schedule if it exists; otherwise return empty slots."""
    _, error = _require_permissions(request, require_view=True)
    if error:
        return error
    _debug(f"GET /api/schedule session_ok")
    
    #Helper to force all IDs to strings
    def stringify_schedule(sched):
        new_sched = {}
        for station, row in sched.items():
            #Cast each ID to string, handling None/0 as empty string
            new_sched[station] = [str(uid) if uid else "" for uid in row]
        return new_sched

    week_param = request.query.get("week")
    resolved = _resolve_schedule_for_date(week_param)
    sched = stringify_schedule(resolved.get("schedule", {}))
    stations = station_names(week_param)
    versions = _load_schedule_versions()
    weeks = sorted([v.get("effective_from") for v in versions if v.get("effective_from") and v.get("effective_from") != _DEFAULT_SCHED_EFFECTIVE], reverse=True)
    if not weeks:
        weeks = [_week_start_iso()]

    payload = {
        "schedule": sched,
        "stations": stations,
        "effective_from": resolved.get("effective_from") if resolved.get("effective_from") != _DEFAULT_SCHED_EFFECTIVE else (week_param or _week_start_iso()),
        "weeks": weeks,
        "meta": resolved.get("meta") or {}
    }
    _debug(f"/api/schedule returning stations={list(sched.keys())} effective_from={payload.get('effective_from')}")

    resp = web.json_response(payload)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return _with_cors(resp, request)

import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta

from .config import settings
from .logger import log_event, log_action  #noqa: F401  #imported for shared use
from .intent_router import IntentRouter, Intent
from .handlers.misc import handle_channel_image_intake as _handle_image_intake, start_profile_scheduler
from .services.show_cache import warm_cache_on_boot
from .services.profile_cache import start_profile_cache_scheduler
from .handlers import feeding as _feed
from .stations import station_names, station_definitions, save_stations_version, station_versions
from UserInterface.sub_request_linker import build_open_sub_requests_view


intent_router = IntentRouter()

SPAM_ALERTS: Dict[int, Dict[str, int]] = {}

#------- Discord intents & bot -------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(command_prefix=settings.command_prefix, intents=intents)

#------- Import real handlers -------
#Cats / Feeding and Dues handlers already use the (intent, ctx) signature
from .handlers.cats import handle_cat_show as _handle_cat_show, handle_cat_photo as _handle_cat_photo
from .handlers.feeding import start_feeding_scheduler, start_morning_scheduler, handle_feeding_inquiry as _handle_feeding_status
#Dues: no background scheduler; admin-only Gmail test is routed directly from the router
from .handlers.dues import start_dues_scheduler
from .handlers.gmail import start_gmail_logging_scheduler
from .services.gallery_retrain import start_gallery_retrain_scheduler, set_gallery_retrain_notifier

from .handlers.admin import handle_silent_mode as _handle_silent_mode_raw
from .handlers.misc import handle_misc as _handle_misc_raw

from .handlers.vision import handle_cv_detect, handle_cv_crop, handle_cv_identify
from .handlers.labeler import get_labeler_routes
from .utils.permissions import is_officer


#--- Muted wrappers: run handlers but drop outbound sends ---
class _MuteChannel:
    """Proxy channel object that logs outbound messages instead of sending."""
    def __init__(self, real, label_fn):
        self._real = real
        self._label_fn = label_fn
        self.id = getattr(real, "id", None)
        self.name = getattr(real, "name", None)

    async def send(self, content=None, **kwargs):
        #Log what would have been sent; don’t actually send.
        from .logger import log_action  #local import to avoid cycles
        #Prefer a short preview of content or note an embed
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
        return None  #mimic coroutine

    def __getattr__(self, name):
        #Delegate unknown attributes/methods to the real channel
        return getattr(self._real, name)

class _MuteMessage:
    """Lightweight message proxy used when silent mode blocks replies."""
    def __init__(self, real_msg, muted_channel):
        #Keep attributes handlers touch; forward everything else if needed
        self._real = real_msg
        self.channel = muted_channel
        self.author = real_msg.author
        #Preserve common identifiers used by router/handlers
        self.id = getattr(real_msg, "id", None)
        self.guild = getattr(real_msg, "guild", None)
        self.content = real_msg.content
        self.clean_content = getattr(real_msg, "clean_content", self.content)
        self.attachments = getattr(real_msg, "attachments", [])

    def __getattr__(self, name):
        #Delegate any other attributes to the real discord.Message
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
    #Guild text channel
    if isinstance(ch, discord.TextChannel):
        return f"#{ch.name}"
    #Thread inside a parent channel; parent can be None so guard it
    if isinstance(ch, discord.Thread):
        parent = getattr(ch, "parent", None)
        parent_prefix = f"#{parent.name}/" if parent and getattr(parent, "name", None) else ""
        return f"{parent_prefix}{ch.name}"
    #1:1 DM (recipient is Optional[User])
    if isinstance(ch, discord.DMChannel):
        return "DM"
    #Group DM, Stage, Voice, PartialMessageable, whatever else
    name = getattr(ch, "name", None)
    return f"#{name}" if isinstance(name, str) and name else ch.__class__.__name__.lower()


def _format_date_for_notification(date_iso: str) -> str:
    """Formats an ISO date string into 'Weekday, MM/DD/YYYY'."""
    try:
        #Handles both 'YYYY-MM-DD' and 'YYYY-MM-DDTHH:MM:SS'
        dt = datetime.fromisoformat(date_iso.split('T')[0])
        dow = dt.strftime("%A")
        date_pretty = dt.strftime("%m/%d/%Y")
        return f"{dow}, {date_pretty}"
    except (ValueError, TypeError):
        return date_iso #Fallback to original string if something is wrong




#------- Adapters to unify handler signatures -------
async def handle_cat_show(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_cat_show(intent, ctx)

async def handle_feeding_status(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_feeding_status(intent, ctx)

async def handle_dues_notice(intent: Intent, ctx: Dict[str, Any]) -> None:
    #Deprecated placeholder; kept for compatibility if referenced elsewhere
    pass

#Admin handler expects (args, ctx) where args == intent.data
async def handle_silent_mode(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_silent_mode_raw(intent.data, ctx)

#Misc handler expects (message, *, now_ts, allow_in_channels)
async def handle_misc(intent: Intent, ctx: Dict[str, Any]) -> None:
    message: discord.Message = ctx["message"]
    await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)

async def handle_cat_profile(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_cat_show(intent, ctx)

async def handle_cat_photo(intent: Intent, ctx: Dict[str, Any]) -> None:
    await _handle_cat_photo(intent, ctx)



#invites_cache
message_cache: dict[int, str] = {}
invites_cache: dict[int, dict[str, int]] = {}
_invite_refresh_locks: dict[int, asyncio.Lock] = {}
_invite_refresh_ts: dict[int, float] = {}
INVITE_REFRESH_COOLDOWN_SEC = int(os.getenv("INVITE_REFRESH_COOLDOWN_SEC", "10") or "10")

async def _fetch_invites(guild: discord.Guild, *, force: bool = False) -> Optional[list[discord.Invite]]:
    """Fetch invites with a per-guild cooldown to avoid rate limits."""
    if not guild.me.guild_permissions.manage_guild:
        print(f"Warning: Missing 'Manage Server' permission in '{guild.name}' to track invites.")
        return None
    gid = int(getattr(guild, "id", 0) or 0)
    if not gid:
        return None
    lock = _invite_refresh_locks.setdefault(gid, asyncio.Lock())
    async with lock:
        now = time.time()
        last = _invite_refresh_ts.get(gid, 0.0)
        if not force and (now - last) < INVITE_REFRESH_COOLDOWN_SEC:
            return None
        _invite_refresh_ts[gid] = now
        try:
            return await guild.invites()
        except Exception:
            return None

async def _refresh_invites(guild: discord.Guild, *, force: bool = False) -> None:
    """Refreshes the invite cache for a given guild."""
    invites = await _fetch_invites(guild, force=force)
    if not invites:
        return
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


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """Limit requests per client to reduce abuse of the web API."""
    is_labeler_api = request.path.startswith("/api/labeler") and request.method != "OPTIONS"
    labeler_session: dict | None = None
    if is_labeler_api:
        session = _get_session_from_request(request)
        if not session:
            return _with_cors(web.Response(status=401, text="Missing or invalid session"), request)
        request["tc_session"] = session
        labeler_session = session
        perms = session.get("permissions", {}) or {}
        if not (perms.get("is_officer") or perms.get("can_label_photos")):
            return _with_cors(web.Response(status=403, text="Not authorized for labeler"), request)

    client_ip = request.remote
    if not client_ip:
        peername = request.transport.get_extra_info("peername") if request.transport else None
        if isinstance(peername, (tuple, list)) and peername:
            client_ip = peername[0]
        elif isinstance(peername, str):
            client_ip = peername
    client_id = client_ip or "unknown"
    max_requests = RATE_LIMIT_MAX_REQUESTS
    if is_labeler_api:
        user_id = str((labeler_session or {}).get("user_id") or "").strip() or client_id
        client_id = f"labeler:{user_id}"
        if request.path.startswith("/api/labeler/cached_image/"):
            max_requests = LABELER_IMAGE_RATE_LIMIT_MAX_REQUESTS
        else:
            max_requests = LABELER_RATE_LIMIT_MAX_REQUESTS
    now = time.time()
    bucket = _rate_limit_counters.setdefault(client_id, deque())
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= max_requests:
        return _with_cors(web.Response(status=429, text="Too many requests"), request)
    bucket.append(now)
    return await handler(request)


async def start_web_server(bot):
    """Simple web server to serve the UI and API."""
    app = web.Application(middlewares=[rate_limit_middleware])

    async def get_index(request):
        """Serve the index.html file."""
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                resp = web.Response(text=f.read(), content_type="text/html")
                resp.headers["Cache-Control"] = "no-store"
                return _with_cors(resp, request)
        except FileNotFoundError:
            return web.Response(text="index.html not found. Please upload it to the bot root.", status=404)

    async def get_labeler_js(request):
        """Serve the labeler.js file."""
        try:
            with open("labeler.js", "r", encoding="utf-8") as f:
                resp = web.Response(text=f.read(), content_type="application/javascript")
                resp.headers["Cache-Control"] = "no-store"
                return _with_cors(resp, request)
        except FileNotFoundError:
            return web.Response(text="labeler.js not found", status=404)

    async def get_members(request):
        """Return JSON list of members allowed to be scheduled."""
        _, error = _require_permissions(request, require_view=True)
        if error:
            return error
        FEEDING_TEAM_ROLE_ID = None
        DUE_PAYING_ROLE_ID = int(getattr(settings, "role_due_paying_id", 0) or 0)
        HOLIDAY_FEEDER_ROLE_ID = int(getattr(settings, "role_holiday_feeder_id", 0) or 0)
        found_members = []
        
        for guild in bot.guilds:
            for role_id in (DUE_PAYING_ROLE_ID, HOLIDAY_FEEDER_ROLE_ID):
                role = guild.get_role(role_id) if role_id else None
                if not role:
                    continue
                for member in role.members:
                    #Use display_name (nickname) if available, fallback to username
                    name = member.display_name
                    found_members.append({
                        "name": name,
                        "user": member.name,
                        "id": str(member.id),
                        "color": str(member.color) if member.color else "#000000"
                    })

        #Deduplicate by ID in case multiple guilds are involved
        unique_members = {m['id']: m for m in found_members}.values()
        #Sort alphabetically
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

    async def options_stations(request):
        return _with_cors(web.Response(status=204), request)

    async def options_auth_token(request):
        return _with_cors(web.Response(status=204), request)

    async def options_auth_session(request):
        return _with_cors(web.Response(status=204), request)

    async def get_stations_api(request):
        """Return station definitions for a given date (default: today) plus available versions."""
        _, error = _require_permissions(request, require_view=True)
        if error:
            return error
        date_param = request.query.get("date")
        versions = station_versions()
        return _with_cors(web.json_response({
            "stations": station_definitions(date_param),
            "versions": versions
        }), request)

    async def save_stations_api(request):
        """Replace station definitions for an effective date; officer only."""
        session, error = _require_permissions(request, require_view=True, require_edit=True)
        if error:
            return error
        csrf_error = _require_csrf(request, session)
        if csrf_error:
            return csrf_error
        try:
            data = await request.json()
        except Exception:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)
        stations_payload = data.get("stations")
        effective_from = data.get("effective_from") or data.get("date")
        if not isinstance(stations_payload, list):
            return _with_cors(web.Response(status=400, text="Stations must be a list"), request)
        if not effective_from:
            return _with_cors(web.Response(status=400, text="effective_from is required"), request)
        try:
            versions = save_stations_version(stations_payload, effective_from)
        except Exception as e:
            logging.exception("Failed to save stations")  #Logs the full stack trace
            return _with_cors(
                web.Response(status=500, text="Failed to save stations due to an internal error."), request
            )
        #Reload and return updated list
        return _with_cors(web.json_response({"stations": station_definitions(effective_from), "versions": versions}), request)

    async def options_subrequest(request):
        return _with_cors(web.Response(status=204), request)

    async def options_sub_open(request):
        return _with_cors(web.Response(status=204), request)

    async def options_sub_claim(request):
        return _with_cors(web.Response(status=204), request)

    async def options_feeding_checklist(request):
        return _with_cors(web.Response(status=204), request)

    async def get_feeding_checklist(request):
        """Officer-only: read local feeding checklist within a date range."""
        _, error = _require_permissions(request, require_edit=True)
        if error:
            return error
        q_from = request.query.get("from")
        q_to = request.query.get("to")
        payload = _feed.get_feeding_snapshot(q_from, q_to)
        return _with_cors(web.json_response(payload), request)

    async def save_feeding_checklist(request):
        """Officer-only: replace station fed/unfed state for a date."""
        session, error = _require_permissions(request, require_edit=True)
        if error:
            return error
        csrf_error = _require_csrf(request, session)
        if csrf_error:
            return csrf_error
        try:
            data = await request.json()
        except Exception:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

        date_iso = data.get("date")
        status_map = data.get("status") or {}
        if not date_iso or not isinstance(status_map, dict):
            return _with_cors(web.Response(status=400, text="Missing required fields"), request)

        try:
            status_clean = {_feed._canonical_station(k) or k: bool(v) for k, v in status_map.items()}
            await _feed.set_feeding_day_status(date_iso, status_clean)
            snap = _feed.get_feeding_snapshot(date_iso, date_iso)
            return _with_cors(web.json_response(snap), request)
        except ValueError:
            return _with_cors(web.Response(status=400, text="Invalid date format"), request)
        except Exception as e:
            logging.exception("Error saving feeding checklist")
            return _with_cors(web.Response(status=500, text="Save failed due to an internal error"), request)

    async def submit_subrequest(request):
        """Record a manual sub request."""
        session, error = _require_permissions(request, require_view=True)
        if error: return error
        csrf_error = _require_csrf(request, session)
        if csrf_error:
            return csrf_error
        
        try:
            data = await request.json()
        except Exception:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

        #--- SECURITY / IMPERSONATION LOGIC ---
        req_user_id = data.get("user_id")
        req_user_name = data.get("user_name")
        
        #If user is NOT an officer, force them to use their own identity
        permissions = session.get("permissions", {})
        if not permissions.get("is_officer"):
            req_user_id = session.get("user_id")
            req_user_name = session.get("username") #fallback
        
        #If officer didn't provide a specific user (standard submit), default to self
        if not req_user_id:
            req_user_id = session.get("user_id")
        if not req_user_name:
            req_user_name = session.get("username")

        raw_requests = data.get("requests")
        parsed_requests = []

        if isinstance(raw_requests, list) and raw_requests:
            for entry in raw_requests:
                date_iso = entry.get("date")
                stations = entry.get("stations") or []
                if not date_iso or not stations:
                    continue
                try:
                    date_obj = datetime.fromisoformat(str(date_iso))
                    date_iso_clean = date_obj.date().isoformat()
                except ValueError:
                    continue
                stations_clean = [s for s in stations if s]
                if stations_clean:
                    parsed_requests.append({
                        "date": date_iso_clean,
                        "stations": list(dict.fromkeys(stations_clean)),
                    })
        else:
            date_iso = data.get("date")
            stations = data.get("stations") or []
            if not req_user_id or not date_iso or not stations:
                return _with_cors(web.Response(status=400, text="Missing required fields"), request)
            try:
                date_obj = datetime.fromisoformat(str(date_iso))
                date_iso_clean = date_obj.date().isoformat()
            except ValueError:
                return _with_cors(web.Response(status=400, text="Invalid date format"), request)
            stations_clean = [s for s in stations if s]
            parsed_requests.append({
                "date": date_iso_clean,
                "stations": list(dict.fromkeys(stations_clean)),
            })

        if not req_user_id or not parsed_requests:
            return _with_cors(web.Response(status=400, text="Missing required fields"), request)

        batch_id = f"sub-{int(datetime.now().timestamp()*1000)}"
        created_ts = datetime.now().isoformat()
        records_written = []

        #Append each date as its own record so the claim UI stays simple
        try:
            from .handlers.feeding import _sub_month_key_from_date, _sub_log_path_from_key
            for idx, req in enumerate(parsed_requests, start=1):
                date_iso = req["date"]
                stations = req["stations"]
                month_key = _sub_month_key_from_date(date_iso) or datetime.now().strftime("%Y-%m")
                path = _sub_log_path_from_key(month_key)
                os.makedirs(os.path.dirname(path), exist_ok=True)

                record = {
                    "kind": "sub_request",
                    "id": f"{batch_id}-{idx}",
                    "parent_id": batch_id,
                    "station": ", ".join(stations),
                    "stations": stations,
                    "dates": [date_iso],
                    "requester": str(req_user_id),  #Force string to future-proof
                    "requester_name": req_user_name,
                    "assignee": None,
                    "status": "requested",
                    "created_at": created_ts,
                    "trigger_phrase": "ui_sub_request",
                }
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                records_written.append(record)
        except Exception as e:
            logging.exception("Exception occurred while writing sub request log")
            return _with_cors(web.Response(status=500, text="An internal error occurred"), request)

        #Notify Discord once for the batch
        try:
            channel_id = getattr(settings, "ch_feeding_team", None)
            if channel_id:
                ch = bot.get_channel(int(channel_id))
                if hasattr(ch, 'send'):
                    lines = [f"<@{req_user_id}> requested substitutes:"]
                    for rec in records_written:
                        stations_str = rec.get("station") or ", ".join(rec.get("stations") or [])
                        date_iso = (rec.get("dates") or [""])[0]
                        pretty_date = _format_date_for_notification(date_iso)
                        lines.append(f"- {pretty_date}: **{stations_str}**")
                    view = build_open_sub_requests_view()
                    await ch.send("\n".join(lines), view=view)
        except Exception:
            pass

        return _with_cors(web.json_response({"status": "ok"}), request)
    
    async def delete_subrequest(request):
        """Physically remove a request from the log file."""
        session, error = _require_permissions(request, require_view=True)
        if error: return error
        csrf_error = _require_csrf(request, session)
        if csrf_error:
            return csrf_error

        try:
            data = await request.json()
        except:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)

        target_id = data.get("id")
        date_iso = data.get("date")

        if not target_id or not date_iso:
            return _with_cors(web.Response(status=400, text="Missing ID or Date"), request)

        try:
            date_obj = datetime.fromisoformat(str(date_iso))
            date_iso = date_obj.date().isoformat()
        except ValueError:
            return _with_cors(web.Response(status=400, text="Invalid date format"), request)

        from .handlers.feeding import _sub_month_key_from_date, _sub_log_path_from_key
        month_key = _sub_month_key_from_date(date_iso)
        path = _sub_log_path_from_key(month_key)

        if not os.path.exists(path):
            return _with_cors(web.Response(status=404, text="Record not found"), request)

        #Re-write file excluding the item
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
                            #Permission check with strict ID comparison
                            requester_id = str(rec.get("requester") or "")
                            assignee_id = str(rec.get("assignee") or "")
                            is_owner = (user_id_str == requester_id) or (user_id_str == assignee_id)

                            if not is_officer and not is_owner:
                                return _with_cors(web.Response(status=403, text="You can only delete your own items."), request)
                            deleted_item = rec
                            continue #Skip this line (Delete)
                        new_lines.append(line)
                    except:
                        new_lines.append(line)
            
            if deleted_item:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                
                #Notify Discord
                ch_id = getattr(settings, "ch_feeding_team", None)
                if ch_id:
                    ch = bot.get_channel(int(ch_id))
                    if hasattr(ch, 'send'):
                        actor_id = session.get("user_id")
                        actor_name = session.get("username", "Unknown")
                        actor_label = f"<@{actor_id}>" if actor_id else actor_name
                        kind = "Request" if deleted_item.get("kind") == "sub_request" else "Claim"
                        st = deleted_item.get("station") or "Unknown"
                        pretty_date = _format_date_for_notification(date_iso)
                        await ch.send(f"**{kind} Deleted**: {actor_label} removed the item for **{st}** on {pretty_date}.")

                return _with_cors(web.json_response({"status": "ok"}), request)
            else:
                return _with_cors(web.Response(status=404, text="Item ID not found in log"), request)

        except Exception as e:
            logging.exception("Error deleting subrequest")
            return _with_cors(web.Response(status=500, text="An internal error has occurred."), request)
    
    async def leave_activity(request):
        """Disconnects the requesting user from their voice channel."""
        session, error = _require_permissions(request, require_view=True)
        if error: return error
        csrf_error = _require_csrf(request, session)
        if csrf_error:
            return csrf_error
        
        user_id = session.get("user_id")
        if not user_id:
            return _with_cors(web.Response(status=400, text="No user"), request)

        #Find the member in the guild
        guild, member = await _resolve_member(int(user_id))
        
        if member and member.voice:
            try:
                #This disconnects them from Voice
                await member.move_to(None)
                _debug(f"Disconnected user {user_id} from voice.")
            except Exception as e:
                print(f"[Activity] Failed to disconnect {user_id}: {e}")
                
        return _with_cors(web.json_response({"status": "ok"}), request)
    
    async def list_open_subs(request):
        """List sub requests separated into available, upcoming filled, and past (expired/fulfilled)."""
        _, error = _require_permissions(request, require_view=True)
        if error:
            return error
        import json
        from datetime import datetime
        from .handlers import feeding as _feed
        today = datetime.now().date()

        accepted_map = {}  #(parent_id, station, date_iso) -> assignee_id
        accepted_meta = {}  #(parent_id, station, date_iso) -> requester_name
        requested_items = []
        missing_requester_ids = set()
        missing_assignee_ids = set()

        #Load using the shared feeding helpers so paths are consistent with the bot
        files = _feed._load_sub_files(
            month_keys=None,
            include_legacy=_feed.SUBS_LEGACY_FILE.exists(),
        )
        for path, rows in files:
            try:
                for rec in rows:
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
                                "requester_id": str(requester) if requester else "", #Ensure string
                                "requester_name": requester_name,
                                "assignee_id": None,
                                "assignee_name": rec.get("assignee_name") or "",
                            })
            except Exception:
                continue

        #Resolve display names for any requester IDs that were missing names in the log.
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
                        user = bot.get_user(uid)  #type: ignore[name-defined]
                        if not user and hasattr(bot, "fetch_user"):
                            try:
                                user = await bot.fetch_user(uid)  #type: ignore[attr-defined]
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
                        user = bot.get_user(uid)  #type: ignore[name-defined]
                        if not user and hasattr(bot, "fetch_user"):
                            try:
                                user = await bot.fetch_user(uid)  #type: ignore[attr-defined]
                            except Exception:
                                user = None
                        if user:
                            display = getattr(user, "global_name", None) or getattr(user, "name", None) or ""
                except Exception:
                    display = ""
                if display:
                    assignee_name_cache[uid] = display

        #After reading all records, populate requester/assignee names and assignee ids from the accept map
        for item in requested_items:
            key = (item.get("id"), item.get("station"), item.get("date"))
            assignee = accepted_map.get(key)
            if assignee:
                item["assignee_id"] = str(assignee) #Ensure string
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

            #Avoid timezone-induced date shifting in browsers: anchor date to noon
            safe_date = date_iso
            if date_iso and len(str(date_iso)) == 10 and "T" not in str(date_iso):
                safe_date = f"{date_iso}T12:00:00"

            out = dict(item)
            out["date_raw"] = date_iso
            out["date"] = safe_date
            #CRITICAL FIX: Strict string conversion for output
            if out.get("requester_id"):
                out["requester_id"] = str(out["requester_id"])
            if out.get("requester"):
                out["requester"] = str(out["requester"])
                
            if assignee:
                out["assignee_id"] = str(assignee)
                if not out.get("assignee_name"):
                    try:
                        out["assignee_name"] = assignee_name_cache.get(int(assignee), "")
                    except Exception:
                        out["assignee_name"] = ""
            
            target_list.append(out)

        resp = web.json_response({
            "available": available,
            "upcoming_filled": upcoming_filled,
            "past": past,
        })
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return _with_cors(resp, request)

    async def claim_subs(request):
        """Mark sub requests as accepted by a user."""
        session, error = _require_permissions(request, require_view=True)
        if error:
            return error
        csrf_error = _require_csrf(request, session)
        if csrf_error:
            return csrf_error
        try:
            data = await request.json()
        except Exception:
            return _with_cors(web.Response(status=400, text="Invalid JSON"), request)
        permissions = session.get("permissions", {})
        user_id = data.get("user_id") or session.get("user_id")
        if not permissions.get("is_officer"):
            user_id = session.get("user_id")
        picks = data.get("picks") or []
        if not user_id or not picks:
            return _with_cors(web.Response(status=400, text="Missing user_id or picks"), request)

        from datetime import datetime
        now_iso = datetime.now().isoformat()
        messages_by_date = {}  #date_iso -> list of (station, requester_id, requester_name)

        for pick in picks:
            parent_id = pick.get("id")
            station = pick.get("station")
            date_iso = pick.get("date")
            requester = pick.get("requester_id")
            requester_name = pick.get("requester_name") or ""
            if not parent_id or not station or not date_iso:
                continue
            try:
                #Locate log file by month
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
                    "requester": str(requester) if requester else "", #Ensure string
                    "assignee": str(user_id), #Ensure string
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

        #Notify feeding team channel
        try:
            channel_id = getattr(settings, "ch_feeding_team", None)
            if channel_id and messages_by_date:
                ch = bot.get_channel(int(channel_id))
                from discord.abc import Messageable
                if isinstance(ch, Messageable):
                    #Build a single aggregated message
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
        web.get('/labeler.js', get_labeler_js),
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
        web.get('/api/stations', get_stations_api),
        web.post('/api/stations', save_stations_api),
        web.options('/api/stations', options_stations),
        web.get('/api/feeding/checklist', get_feeding_checklist),
        web.post('/api/feeding/checklist', save_feeding_checklist),
        web.options('/api/feeding/checklist', options_feeding_checklist),
        web.post('/api/subrequest', submit_subrequest),
        web.options('/api/subrequest', options_subrequest),
        web.get('/api/subs/open', list_open_subs),
        web.options('/api/subs/open', options_sub_open),
        web.post('/api/subs/claim', claim_subs),
        web.options('/api/subs/claim', options_sub_claim),
        web.post('/api/subrequest/delete', delete_subrequest),
        web.options('/api/subrequest/delete', options_subrequest), 
        web.post('/api/activity/leave', leave_activity),
        web.options('/api/activity/leave', options_subrequest),
        #Labeler API routes
        *get_labeler_routes(),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    #Listen on localhost to avoid exposing the UI externally by default
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    print("[TomCat-UI] Web server starting on http://localhost:8080")
    await site.start()


#------- Lifecycle -------
@bot.event
async def on_ready():
    """Discord callback fired once the bot connects."""
    print(f"[TomCat] Logged in as {bot.user} in {len(bot.guilds)} guild(s).")
    #Machine + human “ONLINE” handled by logger.log_event
    log_event({
        "event": "online",
        "user": str(bot.user),
        "guild_count": len(bot.guilds),
    })

    #Startup health checks (file logs only)
    async def _health_checks():
        try:
            #Check image intake tabs
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

    _start_background_task("health_checks", lambda: _health_checks())

    #Seed invite caches for all guilds (for join attribution)
    try:
        for g in bot.guilds:
            try:
                await _refresh_invites(g)
            except Exception:
                pass
    except Exception:
        pass
    _start_background_task("web_server", lambda: start_web_server(bot))
    _start_background_task("profile_scheduler", lambda: start_profile_scheduler(bot))
    #Warm the show-photo cache in background
    try:
        _start_background_task("show_cache_warm", lambda: warm_cache_on_boot())
    except Exception:
        pass
    #start feeding scheduler after the bot is ready and loop is running
    _start_background_task("feeding_scheduler", lambda: start_feeding_scheduler(bot))
    _start_background_task("morning_scheduler", lambda: start_morning_scheduler(bot))
    #Start Gmail logging scheduler if enabled
    try:
        if getattr(settings, "gmail_enabled", False):
            _start_background_task("gmail_scheduler", lambda: start_gmail_logging_scheduler(bot))
        if getattr(settings, "dues_enabled", True):
            _start_background_task("dues_scheduler", lambda: start_dues_scheduler(bot))
    except Exception:
        pass
    #Start catabase profile cache scheduler
    try:
        _start_background_task("profile_cache_scheduler", lambda: start_profile_cache_scheduler())
    except Exception:
        pass
    #Wire gallery retrain notifications into CH_LOGGING.
    async def _notify_gallery_retrain(msg: str) -> None:
        text = str(msg or "").strip()
        if not text:
            return
        ch_id = int(getattr(settings, "ch_logging", 0) or 0)
        if ch_id <= 0:
            return
        ch = bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await bot.fetch_channel(ch_id)
            except Exception as e:
                log_action("gallery_retrain_notify_error", f"fetch_ch={ch_id}", str(e))
                return
        if not hasattr(ch, "send"):
            log_action("gallery_retrain_notify_error", f"ch={ch_id}", "not_messageable")
            return
        try:
            await ch.send(text[:1900])
        except Exception as e:
            log_action("gallery_retrain_notify_error", f"ch={ch_id}", str(e))
    try:
        set_gallery_retrain_notifier(_notify_gallery_retrain)
    except Exception:
        pass
    #Start opt-in gallery retrain scheduler (runs only when explicitly scheduled via UI)
    try:
        _start_background_task("gallery_retrain_scheduler", lambda: start_gallery_retrain_scheduler())
    except Exception:
        pass


#------- Message entrypoint -------
@bot.event
async def on_message(message: discord.Message):
    """Main message hook: run anti-spam and route intents."""
    if message.author.bot:
        return


    #Human + machine log of the incoming message
    log_event({
        "event": "message",
        "author": _user_label(message.author),
        "channel": _channel_label(message.channel),
        "content": message.clean_content if isinstance(message.content, str) else "",
        "attachments": len(message.attachments) if hasattr(message, "attachments") else 0,
    })

    #Spam protection (text + heuristics + NLP backstop for new/untrusted accounts)
    from .spam import check_spam
    spam_flag, reason = check_spam(message, settings)
    if spam_flag:
        #Log and notify in logging channel, then delete the message
        try:
            #Delete spam message (best-effort)
            try:
                await message.delete()
                decision = "deleted"
            except Exception as delete_exc:
                decision = "kept"
                log_action("spam_delete_error", f"ch={_channel_label(message.channel)}", str(delete_exc))

            #Write log line
            log_event({
                "event": "spam",
                "user": _user_label(message.author),
                "channel": _channel_label(message.channel),
                "content": message.clean_content if isinstance(message.content, str) else "",
                "decision": decision,
                "reason": reason,
            })

            #Notify moderators in CH_LOGGING
            log_ch_id = getattr(settings, 'ch_logging', None)
            if log_ch_id:
                ch = message.guild.get_channel(int(log_ch_id)) if message.guild else None
                if not ch:
                    ch = bot.get_channel(int(log_ch_id))
                if ch and hasattr(ch, 'send'):
                    officer_role_id = OFFICER_ROLE_ID
                    mention = f"<@&{int(officer_role_id)}>" if officer_role_id else ""
                    uname = f"@{getattr(message.author,'name','unknown-user')}"
                    body = (
                        "Spam Message Detected\n"
                        f"User: {uname} ({getattr(message.author,'id','')})\n"
                        "Message:\n"
                        f"{message.content or ''}\n\n"
                        f"{mention}\n"
                        "🔨 to ban user\n"
                        "✅ to mark as no threat"
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
                            await alert_msg.add_reaction('🔨')
                            await alert_msg.add_reaction('✅')
                            target_id = int(getattr(message.author, 'id', 0) or 0)
                            guild_id = int(getattr(message.guild, 'id', 0) or 0)
                            if target_id:
                                SPAM_ALERTS[alert_msg.id] = {"user_id": target_id, "guild_id": guild_id}
                        except Exception as react_exc:
                            log_action("spam_alert_react_error", "add_reaction", str(react_exc))
        except Exception:
            pass
        return
    #Channel/DM → Sheet image intake
    try:
        if getattr(message, "attachments", None):
            in_map = settings.channel_sheet_map and int(getattr(message.channel, "id", 0) or 0) in settings.channel_sheet_map
            is_dm = getattr(message, "guild", None) is None
            if in_map or is_dm:
                await _handle_image_intake(message)
    except Exception as e:
        log_action("image_intake_error", f"channel={getattr(message.channel,'id','?')}", str(e))

    #Lightweight fun triggers (e.g., "meow") anywhere; safe_send respects silent mode
    try:
        await _handle_misc_raw(message, now_ts=time.time(), allow_in_channels=None)
    except Exception:
        pass

    #Build ctx once
    ctx: Dict[str, Any] = {
        "bot": bot,
        "message": message,
        "channel": message.channel,
        "author": message.author,
    }

    #Global mute: while silent_mode is ON, route everything through a MuteChannel/Message
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
    #General reaction logging (keeps legacy behavior)
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
    #CV identify feedback reactions (✅/❌) are persisted for gallery retrain + manual dispute queue.
    try:
        from .services.vision_feedback import process_identify_reaction
        reactor_name = _user_label(getattr(payload, "member", None)) or str(payload.user_id)
        handled_cv_feedback = await asyncio.to_thread(
            process_identify_reaction,
            reply_message_id=int(payload.message_id),
            emoji=str(payload.emoji),
            reactor_user_id=int(payload.user_id),
            reactor_name=reactor_name,
        )
        if handled_cv_feedback:
            return
    except Exception as e:
        log_action("viz_feedback_reaction_error", f"msg={payload.message_id}", str(e))
    data = SPAM_ALERTS.get(payload.message_id)
    if not data:
        return
    emoji_str = str(payload.emoji)
    
    #Handle checkmark = "not spam" - remove the hammer react so no one accidentally clicks it
    if emoji_str in {'✅', '✔', '✔️'}:
        try:
            ch = bot.get_channel(payload.channel_id)
            if ch:
                msg = await ch.fetch_message(payload.message_id)
                await msg.remove_reaction('🔨', bot.user)
        except Exception:
            pass
        SPAM_ALERTS.pop(payload.message_id, None)
        log_action('spam_safe', f"msg={payload.message_id}", f"by={payload.user_id}")
        return
    
    #Only handle hammer for ban (ignore legacy X and other reactions)
    if emoji_str not in {'🔨', '🛠', '🛠️'}:
        return
    guild_id = payload.guild_id or data.get('guild_id', 0)
    guild = bot.get_guild(guild_id) if guild_id else None
    if not guild:
        return
    #Prevent the accused user from banning themselves via reaction
    if payload.user_id == data.get('user_id'):
        return
    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except Exception:
        member = None
    if not member:
        return
    can_ban = is_officer(member, settings)
    if not can_ban:
        log_action("spam_ban_denied", f"user={payload.user_id}", f"emoji={emoji_str}")
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


#------- Edit/Delete logging -------
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


#------- Member join/leave + invite tracking -------
@bot.event
async def on_member_join(member: discord.Member):
    """Track invite usage and log onboarding events."""
    try:
        guild = member.guild
        #Compute account age in days
        created = getattr(member, 'created_at', None)
        from datetime import timezone
        age_days = None
        if created:
            try:
                now = datetime.now(timezone.utc)
                age_days = (now - created).days
            except Exception:
                age_days = None

        #Detect which invite increased
        code_used = None
        inviter_id = None
        try:
            before = invites_cache.get(guild.id, {})
            invites = await _fetch_invites(guild)
            if invites:
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
        #Compare role IDs
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

#Optional: parity command (kept tiny)
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
    log_formatter = logging.Formatter(
        "[{asctime}] [{levelname}] {name}: {message}",
        "%Y-%m-%d %H:%M:%S",
        style="{",
    )
    bot.run(settings.discord_token, log_formatter=log_formatter)

if __name__ == "__main__":
    run()
