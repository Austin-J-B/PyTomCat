from __future__ import annotations
import re, random
import discord
import asyncio
from typing import Any, Dict, cast
from ..logger import log_action
from ..config import settings
from ..services.catsheets import build_profile_embed
from ..services.sheets_client import sheets_client
import datetime as dt
from discord.abc import Messageable

try:
    from ..utils.sender import safe_send  # canonical signature (ch, text) -> Awaitable[None]
except Exception:
    async def safe_send(ch, text):
        await ch.send(text)


# Precompile once
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

def _cool(user_id: int, now: float) -> bool:
    last = _COOLDOWN.get(user_id, 0.0)
    if now - last < _COOLDOWN_SECONDS:
        return False
    _COOLDOWN[user_id] = now
    return True

async def _profiles_channel(message: discord.Message, ctx: Dict[str, Any]) -> Messageable | None:
    ch_id = getattr(settings, "ch_member_names", None)
    if not ch_id:
        log_action("profiles_error", "missing_ch_member_names", "")
        return None
    ch = message.guild.get_channel(ch_id) if message.guild else None
    if not ch:
        bot = ctx.get("bot")
        ch = bot.get_channel(ch_id) if bot else None
    return ch if isinstance(ch, Messageable) else None

def _open_ws(worksheet_title: str):
    """Open a worksheet by title, preferring the Vision sheet but falling back to Catabase.
    This helps when a tab like TCBPicsInput lives under Catabase, not Vision.
    """
    gc = sheets_client()
    # Try Vision/Aux first
    sh_id = settings.sheet_vision_id or settings.aux_spreadsheet_id
    if sh_id:
        try:
            sh = gc.open_by_key(sh_id)
            return sh.worksheet(worksheet_title)
        except Exception:
            pass
    # Fallback to Catabase
    cat_id = settings.sheet_catabase_id or settings.cat_spreadsheet_id
    if cat_id:
        try:
            sh2 = gc.open_by_key(cat_id)
            return sh2.worksheet(worksheet_title)
        except Exception:
            pass
    log_action("image_intake_error", f"tab={worksheet_title}", "no_worksheet")
    return None


async def handle_profiles_create(intent, ctx):
    """TomCat, create profile(s) <startId> [through <endId>]"""
    msg: discord.Message = ctx["message"]
    author = ctx["author"]
    if not getattr(getattr(author, "guild_permissions", None), "administrator", False):
        return  # admin-only, quiet

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

    # Load CatDatabase once
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

    # Column 0: "67. Microwave", Column 1: numeric ID as string
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
            # Log mapping so you can copy back into config if you want
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
    """TomCat, update profile <id>"""
    msg: discord.Message = ctx["message"]
    author = ctx["author"]
    if not getattr(getattr(author, "guild_permissions", None), "administrator", False):
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
        m = await ch.fetch_message(int(msg_id))
    except Exception as e:
        log_action("profile_update_error", f"id={cat_id}", f"fetch:{e}")
        try:
            await msg.clear_reactions(); await msg.add_reaction("❌")
        except Exception:
            pass
        return

    # Find name by ID
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
        embed = discord.Embed.from_dict(embed_dict)
        await m.edit(embed=embed)
        await msg.clear_reactions(); await msg.add_reaction("✅")
    except Exception as e:
        log_action("profile_update_error", f"id={cat_id}", str(e))
        try:
            await msg.clear_reactions(); await msg.add_reaction("❌")
        except Exception:
            pass

async def handle_profiles_update_all(intent, ctx):
    """TomCat, update all profiles"""
    msg: discord.Message = ctx["message"]
    author = ctx["author"]
    if not getattr(getattr(author, "guild_permissions", None), "administrator", False):
        return
    ch = await _profiles_channel(msg,ctx)
    if not ch:
        return
    try:
        await msg.add_reaction("👍")
    except Exception:
        pass

    # Preload CatDatabase for speed
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

    failed = []
    for cat_id, msg_id in settings.profile_messages.items():
        r = by_id.get(str(cat_id))
        if not r:
            failed.append(str(cat_id)); continue
        cat_name = r[0].split(".", 1)[-1].strip()
        try:
            embed_dict = await build_profile_embed(cat_name)
            if isinstance(embed_dict, str):
                failed.append(str(cat_id)); continue
            embed = discord.Embed.from_dict(embed_dict)
            m = await ch.fetch_message(int(msg_id))
            await m.edit(embed=embed)
        except Exception as e:
            failed.append(str(cat_id))
            log_action("profile_update_error", f"id={cat_id}", str(e))

    try:
        await msg.clear_reactions()
        await msg.add_reaction("✅" if not failed else "⚠️")
    except Exception:
        pass

    if failed:
        log_action("profile_update_failed_ids", f"count={len(failed)}", ",".join(failed))

async def start_profile_scheduler(bot):
    # run daily at ~02:10 local
    target_h, target_m = 2, 10
    while True:
        now = dt.datetime.now()
        nxt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            # fabricate a tiny ctx using the bot and a dummy author; channel is resolved inside
            dummy_ctx = {"bot": bot, "message": type("X", (), {"add_reaction": lambda *_: None})(), "author": type("Y", (), {"guild_permissions": type("Z", (), {"administrator": True})()})()}
            await handle_profiles_update_all(type("Intent", (), {"data": {}}), dummy_ctx)
            log_action("profiles_scheduler", "update_all", "ran")
        except Exception as e:
            log_action("profiles_scheduler_error", "", str(e))

async def handle_channel_image_intake(message: discord.Message) -> None:
    ch_id = getattr(message.channel, "id", None)
    tab = settings.channel_sheet_map.get(int(ch_id)) if ch_id else None
    if not tab:
        return
    images = [a for a in (message.attachments or []) if (a.content_type or "").startswith("image/")]
    if not images:
        return

    try:
        ws = _open_ws(tab)
        if ws is None:
            log_action("image_intake_error", f"channel={ch_id}", "no_worksheet")
            return
        # Per spec: Column A = direct media link, B = username (the @name), C = timestamp (UTC Z)
        username = getattr(message.author, 'name', 'user')
        tsz = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
        rows = [[
            att.url,
            username,
            tsz,
        ] for att in images]
        ws.append_rows(rows, value_input_option=cast(Any,"USER_ENTERED"))
        log_action("image_intake", f"channel={ch_id}", f"rows={len(rows)}")
    except Exception as e:
        log_action("image_intake_error", f"channel={ch_id}", str(e))





async def handle_misc(message: discord.Message, *, now_ts: float, allow_in_channels: set[int] | None = None):
    if message.author.bot:
        return
    if allow_in_channels and message.channel.id not in allow_in_channels:
        return
    content = message.content
    # Skip code blocks to avoid false positives
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
        

