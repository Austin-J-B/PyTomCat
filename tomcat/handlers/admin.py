"""Administrative Discord commands for TomCat (role cleanup, cache resets, timeouts, etc.)."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

import discord

from .. import aliases as ALIAS
from ..config import settings
from ..logger import log_action
from ..services import profile_cache as PC
from ..utils.permissions import is_officer

# --------------- timeout duration parsing ---------------
_DURATION_PART_RE = re.compile(
    r"(\d+)\s*(?:(hours?|hrs?|h)|(minutes?|mins?|m(?!e|s))|(seconds?|secs?|s))",
    re.I,
)


def _parse_duration_seconds(text: str) -> int:
    """Extract total seconds from natural-language duration in text."""
    total = 0
    for m in _DURATION_PART_RE.finditer(text):
        val = int(m.group(1))
        if m.group(2):    # hours
            total += val * 3600
        elif m.group(3):  # minutes
            total += val * 60
        elif m.group(4):  # seconds
            total += val
    if total == 0:
        # Fallback: bare number after "for" -> treat as minutes
        bare = re.search(r"\bfor\s+(\d+)\b", text)
        if bare:
            total = int(bare.group(1)) * 60
    return total


# --------------- timeout budget tracking ---------------
_TZ = ZoneInfo("America/Chicago")
TIMEOUT_LOG_ROOT = Path("logs/moderation/timeouts")
TIMEOUT_MONTHLY_BUDGET = 600  # 10 minutes in seconds

_TIMEOUT_USAGE_MSG = (
    "To use that command, be sure to ping the user in your message "
    "and also include the amount of time you want them to be timed out."
)


def _budget_msg(remaining: int) -> str:
    mins, secs = divmod(remaining, 60)
    return (
        f"Each officer is only allowed to spend 10 minutes of time-out each month. "
        f"Your remaining balance is {mins}m {secs}s."
    )


def _timeout_month_key() -> str:
    now = datetime.now(_TZ)
    return f"{now.year}-{now.month:02d}"


def _timeout_log_path(month_key: str) -> Path:
    year = month_key.split("-")[0]
    folder = TIMEOUT_LOG_ROOT / year
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{month_key}.jsonl"


def _get_officer_month_usage(officer_id: int) -> int:
    """Sum seconds used by this officer in the current month."""
    path = _timeout_log_path(_timeout_month_key())
    if not path.exists():
        return 0
    total = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line.strip())
                if int(row.get("officer_id", 0)) == officer_id:
                    total += int(row.get("seconds", 0))
            except Exception:
                continue
    return total


def _log_timeout(officer_id: int, target_id: int, seconds: int) -> None:
    """Append timeout record to monthly NDJSON."""
    key = _timeout_month_key()
    path = _timeout_log_path(key)
    record = {
        "officer_id": officer_id,
        "target_id": target_id,
        "seconds": seconds,
        "timestamp": datetime.now(_TZ).isoformat(),
        "remaining_budget": TIMEOUT_MONTHLY_BUDGET - (_get_officer_month_usage(officer_id) + seconds),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def handle_silent_mode(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    author = ctx["author"]
    if not is_officer(author, settings):
        log_action("silent_mode_denied", f"user={author.id}", "unauthorized")
        return

    on = bool(args.get("on", False))
    settings.silent_mode = on
    log_action("silent_mode_set", f"user={getattr(author, 'name', author.id)}", "on" if on else "off")
    try:
        await ctx["message"].add_reaction("👍")
    except Exception:
        pass


async def handle_remove_role_from_all(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """Officer-only: remove a specific role from all guild members."""
    message: discord.Message = ctx["message"]
    author = ctx["author"]
    role_id = int(args.get("role_id") or 0)

    if not is_officer(author, settings):
        log_action("role_remove_all_denied", f"user={author.id}", "unauthorized")
        return

    bot = ctx.get("bot")
    target_gid = getattr(settings, "target_guild_id", None)
    guild = None
    try:
        guild = bot.get_guild(int(target_gid)) if bot and target_gid else None
    except Exception:
        guild = None
    if not guild:
        try:
            await message.channel.send("Could not access the main server to remove roles (missing TARGET_GUILD_ID?).")
        except Exception:
            pass
        return

    role = guild.get_role(role_id)
    if not role:
        try:
            await message.channel.send(f"Role {role_id} not found in this server.")
        except Exception:
            pass
        return

    try:
        await message.channel.send(f"Starting role removal: removing <@&{role_id}> from all members who have it...")
    except Exception:
        pass
    removed = 0

    members = list(getattr(guild, "members", []) or [])
    if not members:
        try:
            async for member in guild.fetch_members(limit=None):
                members.append(member)
        except Exception:
            members = list(getattr(guild, "members", []) or [])

    for member in members:
        try:
            has_role = role in getattr(member, "roles", [])
            if not has_role and getattr(member, "get_role", None):
                has_role = bool(member.get_role(role_id))
            if not has_role:
                continue
            try:
                await member.remove_roles(role, reason=f"TomCat: remove role {role_id} from everyone")
                removed += 1
            except Exception as e:
                log_action("role_remove_error", f"uid={getattr(member, 'id', 0)}", str(e))
            await asyncio.sleep(0.3)
        except Exception:
            continue

    try:
        await message.channel.send(f"Done. Removed <@&{role_id}> from {removed} member(s).")
    except Exception:
        pass
    log_action("role_remove_all_done", f"role={role_id}", f"removed={removed}")


async def handle_recache_catabase(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """Officer-only: Refresh the Catabase cache and alias map."""
    message: discord.Message = ctx["message"]
    author = ctx["author"]
    if not is_officer(author, settings):
        log_action("recache_catabase_denied", f"user={author.id}", "unauthorized")
        return
    try:
        await message.channel.send("Refreshing Catabase profiles and names...")
    except Exception:
        pass
    try:
        count = await PC.refresh_async()
        try:
            ALIAS.refresh_aliases_now()
        except Exception:
            pass
        try:
            await message.channel.send(f"Catabase refreshed: {count} profiles.")
        except Exception:
            pass
    except Exception as e:
        try:
            await message.channel.send(f"Catabase refresh error: {e}")
        except Exception:
            pass
        log_action("recache_catabase_error", "", str(e))


async def handle_timeout(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """Officer-only: timeout a Discord member, deducting from monthly budget."""
    message: discord.Message = ctx["message"]
    author = ctx["author"]

    if not is_officer(author, settings):
        log_action("timeout_denied", f"user={author.id}", "unauthorized")
        return

    target_uid = args.get("target_user_id")
    duration_sec = _parse_duration_seconds(args.get("text") or "")

    # Missing mention or duration
    if not target_uid or not duration_sec or duration_sec <= 0:
        try:
            await message.channel.send(_TIMEOUT_USAGE_MSG)
        except Exception:
            pass
        return

    # Check budget (covers both >10min single request and exceeding remaining balance)
    used = _get_officer_month_usage(author.id)
    remaining = TIMEOUT_MONTHLY_BUDGET - used
    if duration_sec > remaining:
        try:
            await message.channel.send(_budget_msg(remaining))
        except Exception:
            pass
        return

    # Fetch guild and member (errors logged silently)
    bot = ctx.get("bot")
    target_gid = getattr(settings, "target_guild_id", None)
    guild = None
    try:
        guild = bot.get_guild(int(target_gid)) if bot and target_gid else None
    except Exception:
        guild = None
    if not guild:
        log_action("timeout_error", f"officer={author.id}", "guild_not_found")
        return

    try:
        member = await guild.fetch_member(target_uid)
    except Exception as e:
        log_action("timeout_error", f"officer={author.id} target={target_uid}", f"member_fetch_failed: {e}")
        return

    # Execute Discord timeout (errors logged silently)
    try:
        await member.timeout(timedelta(seconds=duration_sec), reason=f"Officer timeout by {getattr(author, 'name', author.id)}")
    except Exception as e:
        log_action("timeout_error", f"officer={author.id} target={target_uid}", f"discord_api_failed: {e}")
        return

    # Log to monthly NDJSON
    _log_timeout(author.id, target_uid, duration_sec)

    # Confirm success
    new_remaining = remaining - duration_sec
    mins_d, secs_d = divmod(duration_sec, 60)
    mins_r, secs_r = divmod(new_remaining, 60)
    try:
        await message.channel.send(
            f"Timed out <@{target_uid}> for {mins_d}m {secs_d}s. "
            f"You have {mins_r}m {secs_r}s remaining this month."
        )
    except Exception:
        pass
    log_action("timeout_applied", f"officer={author.id} target={target_uid}", f"seconds={duration_sec} remaining={new_remaining}")
