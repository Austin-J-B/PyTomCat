"""Administrative Discord commands for TomCat (role cleanup, cache resets, etc.)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import discord

from .. import aliases as ALIAS
from ..config import settings
from ..logger import log_action
from ..services import profile_cache as PC
from ..utils.permissions import is_officer


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
