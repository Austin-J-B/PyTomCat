"""Catch-all reactions, channel logging helpers, and misc user commands."""

from __future__ import annotations
import re, random
import json
import hashlib
import discord
import asyncio
from typing import Any, Dict, cast
from ..logger import log_action
from ..config import settings
from ..services.catsheets import build_profile_embed
from ..services.sheets_client import sheets_client
from ..utils.permissions import is_officer
import datetime as dt
from discord.abc import Messageable

try:
    from ..utils.sender import safe_send  #canonical signature (ch, text) -> Awaitable[None]
except Exception:
    async def safe_send(ch, text):
        await ch.send(text)


#Precompile once
MEOWS = [
    "meow!", "MEOW!", "meeeoowww", "meow meow", "mrow!", "mrrp?",
    "meow? :3", "MEOW MEOW!", "*stretches*"
]
from typing import Callable

TRIGGERS: list[tuple[re.Pattern, Callable[[], str]]] = [
    (re.compile(r"\bmeow\b", re.I), lambda: random.choice(MEOWS)),
    (re.compile(r"\bthanks\s+tomcat\b", re.I), lambda: "You're welcome"),
    (re.compile(r"\bthank\s+you\s+tomcat\b", re.I), lambda: "You're welcome"),
]

_COOLDOWN = {}
_COOLDOWN_SECONDS = 1
_profiles_update_lock = asyncio.Lock()
_profile_embed_hashes: dict[str, str] = {}


def _embed_digest(embed_dict: dict) -> str:
    """Stable digest for comparing profile embeds between scheduler runs."""
    try:
        blob = json.dumps(embed_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        blob = str(embed_dict)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _message_embed_digest(message: discord.Message) -> str | None:
    """Digest the first embed currently on a message, if present."""
    try:
        embeds = list(getattr(message, "embeds", []) or [])
        if not embeds:
            return None
        return _embed_digest(embeds[0].to_dict())
    except Exception:
        return None


def _is_unknown_message_error(exc: Exception) -> bool:
    txt = str(exc).lower()
    return ("unknown message" in txt) or ("10008" in txt)

def _cool(user_id: int, now: float) -> bool:
    last = _COOLDOWN.get(user_id, 0.0)
    if now - last < _COOLDOWN_SECONDS:
        return False
    _COOLDOWN[user_id] = now
    return True

async def _profiles_channel(message: discord.Message, ctx: Dict[str, Any]) -> Messageable | None:
    """Resolve which log channel a profile command should output to."""
    ch_id = getattr(settings, "ch_cats_on_campus", None) or getattr(settings, "ch_member_names", None)
    if not ch_id:
        log_action("profiles_error", "missing_profiles_channel", "")
        return None
    guild = getattr(message, "guild", None)
    ch = guild.get_channel(ch_id) if guild else None
    if not ch:
        bot = ctx.get("bot")
        ch = bot.get_channel(ch_id) if bot else None
        if not ch and bot:
            #Fallback: walk guild cache to resolve the channel by ID
            for g in getattr(bot, "guilds", []):
                candidate = g.get_channel(ch_id)
                if candidate:
                    ch = candidate
                    break
    return ch if isinstance(ch, Messageable) else None


async def _scan_guild_for_message(guild: discord.Guild, message_id: int, *, skip_channel_id: int | None = None) -> discord.Message | None:
    """Locate a message by ID by scanning text channels in a guild."""
    for ch in getattr(guild, "text_channels", []):
        if skip_channel_id and int(getattr(ch, "id", 0)) == int(skip_channel_id):
            continue
        try:
            return await ch.fetch_message(int(message_id))
        except Exception as e:
            if _is_unknown_message_error(e):
                continue
            continue
    return None


async def _fetch_profile_message(ctx: Dict[str, Any], primary_channel: Messageable | None, message_id: int) -> discord.Message:
    """Fetch a profile message by ID, with cross-channel fallback when channel config is stale."""
    primary_err: Exception | None = None
    if primary_channel and hasattr(primary_channel, "fetch_message"):
        try:
            return await primary_channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
        except Exception as e:
            primary_err = e
            if not _is_unknown_message_error(e):
                raise

    guilds: list[discord.Guild] = []
    seen: set[int] = set()

    def _push_guild(g: Any) -> None:
        gid = int(getattr(g, "id", 0) or 0)
        if not gid or gid in seen:
            return
        seen.add(gid)
        guilds.append(g)

    _push_guild(getattr(primary_channel, "guild", None))
    _push_guild(getattr(ctx.get("message"), "guild", None))
    bot = ctx.get("bot")
    for g in getattr(bot, "guilds", []):
        _push_guild(g)

    skip_id = int(getattr(primary_channel, "id", 0) or 0) if primary_channel else None
    for g in guilds:
        found = await _scan_guild_for_message(g, int(message_id), skip_channel_id=skip_id)
        if found:
            src = int(getattr(primary_channel, "id", 0) or 0) if primary_channel else 0
            dst = int(getattr(found.channel, "id", 0) or 0)
            if src and dst and src != dst:
                log_action("profiles_channel_relocated", f"from={src}", f"to={dst}; msg={message_id}")
            return found

    if primary_err:
        raise primary_err
    raise RuntimeError(f"Message not found: {message_id}")

def _open_ws(worksheet_title: str, *, preferred_sheet_id: str | None = None):
    """Open a worksheet by title, checking an optional preferred spreadsheet first."""
    gc = sheets_client()
    tried: set[str] = set()

    candidates = []
    if preferred_sheet_id:
        candidates.append(preferred_sheet_id)
    for sid in [settings.sheet_vision_id or settings.aux_spreadsheet_id,
                settings.sheet_catabase_id or settings.cat_spreadsheet_id]:
        if sid and sid not in candidates:
            candidates.append(sid)

    for sid in candidates:
        if not sid or sid in tried:
            continue
        tried.add(sid)
        try:
            sh = gc.open_by_key(sid)
            return sh.worksheet(worksheet_title)
        except Exception:
            continue
    return None


async def handle_profiles_create(intent, ctx):
    """Create a new profile worksheet tab when staff request it."""
    """TomCat, create profile(s) <startId> [through <endId>]"""
    msg: discord.Message = ctx["message"]
    author = ctx["author"]
    if not is_officer(author, settings):
        return

    start_id = int(intent.data.get("start_id"))
    end_id = int(intent.data.get("end_id") or start_id)

    ch = await _profiles_channel(msg, ctx)
    if not ch:
        log_action("profiles_error", "no_profiles_channel", f"{start_id}-{end_id}")
        return

    try:
        await msg.add_reaction("👍")
    except Exception:
        pass

    #Load CatDatabase once
    try:
        gc = sheets_client()
        sheet_id = settings.sheet_catabase_id or settings.cat_spreadsheet_id
        if not sheet_id:
            log_action("profiles_error", "missing_catabase_id", "")
            try:
                await msg.clear_reactions(); await msg.add_reaction("❌")
            except Exception:
                pass
            return
        ws = gc.open_by_key(sheet_id).worksheet("CatDatabase")
        rows = ws.get_all_values()
    except Exception as e:
        log_action("profiles_error", "sheet_read", str(e))
        try:
            await msg.clear_reactions()
            await msg.add_reaction("❌")
        except Exception:
            pass
        return

    header, *data = rows if rows else ([], [])
    made, failed = 0, []

    #Column 0: "67. Microwave", Column 1: numeric ID as string
    for cid in range(start_id, end_id + 1):
        id_str = str(cid)
        r = next((r for r in data if len(r) > 1 and r[1] == id_str), None)
        if not r:
            failed.append(id_str); continue
        cat_name = r[0].split(".", 1)[-1].strip()

        try:
            embed_dict = await build_profile_embed(cat_name)
            if isinstance(embed_dict, str):
                failed.append(id_str); continue
            embed = discord.Embed.from_dict(embed_dict)
            sent = await ch.send(embed=embed)
            made += 1
            #Log mapping so you can copy back into config if you want
            log_action("profile_created", f"id={id_str}", f"msg={sent.id}")
        except Exception as e:
            failed.append(id_str)
            log_action("profile_create_error", f"id={id_str}", str(e))

    try:
        await msg.clear_reactions()
        await msg.add_reaction("✅" if not failed else "⚠️")
    except Exception:
        pass

    if failed:
        log_action("profile_create_failed_ids", f"count={len(failed)}", ",".join(failed))

async def handle_profile_update_one(intent, ctx):
    """Update a single profile row based on user-provided fields."""
    """TomCat, update profile <id>"""
    msg: discord.Message = ctx["message"]
    author = ctx["author"]
    if not is_officer(author, settings):
        return

    cat_id = str(intent.data.get("cat_id"))
    msg_id = settings.profile_messages.get(cat_id)
    if not msg_id:
        log_action("profile_update_error", f"id={cat_id}", "no_saved_message_id")
        return

    ch = await _profiles_channel(msg,ctx)
    if not ch:
        log_action("profiles_error", "no_profiles_channel", cat_id)
        return

    try:
        await msg.add_reaction("👍")
    except Exception:
        pass

    try:
        m = await _fetch_profile_message(ctx, ch, int(msg_id))
        ch = cast(Messageable, m.channel)
    except Exception as e:
        log_action("profile_update_error", f"id={cat_id}", f"fetch:{e}")
        try:
            await msg.clear_reactions(); await msg.add_reaction("❌")
        except Exception:
            pass
        return

    #Find name by ID
    try:
        gc = sheets_client()
        sheet_id = settings.sheet_catabase_id or settings.cat_spreadsheet_id
        if not sheet_id:
            log_action("profiles_error", "missing_catabase_id", "")
            try:
                await msg.clear_reactions(); await msg.add_reaction("❌")
            except Exception:
                pass
            return
        ws = gc.open_by_key(sheet_id).worksheet("CatDatabase")
        rows = ws.get_all_values()
        _, *data = rows if rows else ([], [])
        r = next((r for r in data if len(r) > 1 and r[1] == cat_id), None)
        if not r:
            raise RuntimeError("id_not_found")
        cat_name = r[0].split(".", 1)[-1].strip()
        embed_dict = await build_profile_embed(cat_name)
        if isinstance(embed_dict, str):
            raise RuntimeError(embed_dict)
        desired_digest = _embed_digest(embed_dict)
        current_digest = _message_embed_digest(m)
        if current_digest and current_digest == desired_digest:
            _profile_embed_hashes[cat_id] = desired_digest
            await msg.clear_reactions(); await msg.add_reaction("✅")
            return
        embed = discord.Embed.from_dict(embed_dict)
        await m.edit(embed=embed)
        _profile_embed_hashes[cat_id] = desired_digest
        await msg.clear_reactions(); await msg.add_reaction("✅")
    except Exception as e:
        log_action("profile_update_error", f"id={cat_id}", str(e))
        try:
            await msg.clear_reactions(); await msg.add_reaction("❌")
        except Exception:
            pass

async def handle_profiles_update_all(intent, ctx):
    """Refresh cached profile data for every cat."""
    """TomCat, update all profiles"""
    msg: discord.Message = ctx["message"]
    author = ctx["author"]
    if not is_officer(author, settings):
        return
    ch = await _profiles_channel(msg,ctx)
    if not ch:
        return
    try:
        await msg.add_reaction("👍")
    except Exception:
        pass

    #Preload CatDatabase for speed
    try:
        gc = sheets_client()
        sheet_id = settings.sheet_catabase_id or settings.cat_spreadsheet_id
        if not sheet_id:
            log_action("profiles_error", "missing_catabase_id", "")
            return
        ws = gc.open_by_key(sheet_id).worksheet("CatDatabase")
        rows = ws.get_all_values()
        _, *data = rows if rows else ([], [])
        by_id = {r[1]: r for r in data if len(r) > 1}
    except Exception as e:
        log_action("profiles_error", "sheet_read", str(e))
        return

    if _profiles_update_lock.locked():
        log_action("profiles_scheduler_skip", "update_all", "already_running")
        return

    failed = []
    async with _profiles_update_lock:
        for cat_id, msg_id in settings.profile_messages.items():
            cat_key = str(cat_id)
            r = by_id.get(cat_key)
            if not r:
                failed.append(cat_key); continue
            cat_name = r[0].split(".", 1)[-1].strip()
            try:
                embed_dict = await build_profile_embed(cat_name)
                if isinstance(embed_dict, str):
                    failed.append(cat_key); continue
                digest = _embed_digest(embed_dict)
                if _profile_embed_hashes.get(cat_key) == digest:
                    continue
                m = await _fetch_profile_message(ctx, ch, int(msg_id))
                ch = cast(Messageable, m.channel)
                current_digest = _message_embed_digest(m)
                if current_digest and current_digest == digest:
                    _profile_embed_hashes[cat_key] = digest
                    continue
                embed = discord.Embed.from_dict(embed_dict)
                await m.edit(embed=embed)
                _profile_embed_hashes[cat_key] = digest
                await asyncio.sleep(max(1.0, float(getattr(settings, "profile_update_edit_delay_sec", 1.0) or 1.0)))
            except Exception as e:
                failed.append(cat_key)
                log_action("profile_update_error", f"id={cat_id}", str(e))

    try:
        await msg.clear_reactions()
        await msg.add_reaction("✅" if not failed else "⚠️")
    except Exception:
        pass

    if failed:
        log_action("profile_update_failed_ids", f"count={len(failed)}", ",".join(failed))

async def start_profile_scheduler(bot):
    """Kick off background tasks that sync the profile cache."""
    #run daily at ~02:10 local
    target_h, target_m = 2, 10
    while True:
        now = dt.datetime.now()
        nxt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            #fabricate a tiny ctx using the bot and a dummy officer author; channel is resolved inside
            off_role_id = int(getattr(settings, "officer_role_id", 0) or 0)
            dummy_role = type("R", (), {"id": off_role_id})()
            dummy_author = type("Y", (), {"roles": [dummy_role], "guild_permissions": type("Z", (), {"administrator": False})()})()
            dummy_ctx = {"bot": bot, "message": type("X", (), {"add_reaction": lambda *_: None})(), "author": dummy_author}
            await handle_profiles_update_all(type("Intent", (), {"data": {}}), dummy_ctx)
            log_action("profiles_scheduler", "update_all", "ran")
        except Exception as e:
            log_action("profiles_scheduler_error", "", str(e))

async def handle_channel_image_intake(message: discord.Message) -> None:
    """Log attachments dropped into intake channels (or DMs) to the right Sheets tab."""
    ch_id = getattr(message.channel, "id", None)
    sheet_override: str | None = None
    tab = None
    if ch_id is not None and int(ch_id) in settings.channel_sheet_map:
        tab = settings.channel_sheet_map.get(int(ch_id))

    is_dm = getattr(message, "guild", None) is None
    if not tab and is_dm:
        tab = settings.dm_image_tab or next(iter(settings.channel_sheet_map.values()), None)
        sheet_override = settings.dm_image_sheet_id

    if isinstance(tab, str) and "::" in tab:
        sheet_override, tab = [s.strip() for s in tab.split("::", 1)]
        sheet_override = sheet_override or None

    if tab == "TCBPicsInput" and not sheet_override:
        sheet_override = settings.sheet_catabase_id or settings.cat_spreadsheet_id or sheet_override

    if not tab:
        return
    images = [a for a in (message.attachments or []) if (a.content_type or "").startswith("image/")]
    if not images:
        return

    try:
        ws = _open_ws(tab, preferred_sheet_id=sheet_override)
        if ws is None:
            meta = f"no_worksheet tab={tab}"
            if sheet_override:
                meta += f" sheet={sheet_override}"
            log_action("image_intake_error", f"channel={ch_id or 'dm'}", meta)
            return
        #Per spec: Column A = direct media link, B = username (the @name), C = timestamp (UTC Z)
        username = getattr(message.author, 'name', 'user')
        tsz = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
        rows = [[att.url, username, tsz] for att in images]
        #Force append into columns A:C so values do not drift to F:H when prior columns have formatting
        ws.append_rows(
            rows,
            value_input_option=cast(Any, "USER_ENTERED"),
            table_range="A:C",
        )
        log_action("image_intake", f"channel={ch_id or 'dm'}", f"rows={len(rows)}")
    except Exception as e:
        log_action("image_intake_error", f"channel={ch_id or 'dm'}", str(e))





async def handle_misc(message: discord.Message, *, now_ts: float, allow_in_channels: set[int] | None = None):
    """Fallback handler for lightweight keywords and reactions."""
    if message.author.bot:
        return
    if allow_in_channels and message.channel.id not in allow_in_channels:
        return
    content = message.content
    #Skip code blocks to avoid false positives
    if "```" in content or "`" in content:
        return
    for rx, fn in TRIGGERS:
        m = rx.search(content)
        if m:
            if not _cool(message.author.id, now_ts):
                return
            resp = fn()
            await safe_send(message.channel, resp)
            log_action("handle_misc", f"trigger={m.group(0)}", resp)
            return
        
