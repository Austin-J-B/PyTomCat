"""Administrative Discord commands for TomCat (role cleanup, cache resets, etc.)."""

from __future__ import annotations
import asyncio, os, shutil
import discord
from typing import Dict, Any
from ..config import settings
from ..logger import log_action
from ..services.show_cache import ensure_cat_cache
from ..services.catsheets import sheets_client  # type: ignore
from ..services import profile_cache as PC
from .. import aliases as ALIAS


async def handle_silent_mode(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    author = ctx["author"]
    if int(author.id) not in settings.admin_ids:
        log_action("silent_mode_denied", f"user={author.id}", "unauthorized")
        return

    # Expect args like {"on": True} or {"on": False}
    on = bool(args.get("on", False))
    settings.silent_mode = on
    log_action("silent_mode_set", f"user={getattr(author,'name',author.id)}", "on" if on else "off")
    try:
        await ctx["message"].add_reaction("👍")
    except Exception:
        pass


# Guild-scoped admin actions target the primary CCC server (configurable via TARGET_GUILD_ID).

async def handle_remove_role_from_all(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """Admin-only: remove a specific role from all guild members.
    Only removes the single role; does not modify any other roles.
    args = {"role_id": int}
    """
    message: discord.Message = ctx["message"]
    author = ctx["author"]
    role_id = int(args.get("role_id") or 0)

    # Admin guard
    is_admin = int(getattr(author, 'id', 0)) in (getattr(settings, 'admin_ids', []) or []) or \
               getattr(getattr(author, 'guild_permissions', None), 'administrator', False)
    if not is_admin:
        log_action("role_remove_all_denied", f"user={author.id}", "unauthorized")
        return

    # Always operate on the target main guild, regardless of where the command is invoked
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

    # Confirm and run
    try:
        await message.channel.send(f"Starting role removal: removing <@&{role_id}> from all members who have it…")
    except Exception:
        pass
    removed = 0
    checked = 0

    members = list(getattr(guild, 'members', []) or [])
    if not members:
        try:
            async for m in guild.fetch_members(limit=None):
                members.append(m)
        except Exception:
            members = list(getattr(guild, 'members', []) or [])

    for m in members:
        checked += 1
        try:
            if role in getattr(m, 'roles', []) or getattr(m, 'get_role', None) and m.get_role(role_id):
                try:
                    await m.remove_roles(role, reason=f"TomCat: remove role {role_id} from everyone")
                    removed += 1
                except Exception as e:
                    log_action("role_remove_error", f"uid={getattr(m,'id',0)}", str(e))
                await asyncio.sleep(0.3)
        except Exception:
            continue
        # Reduce chatter: rely on logs; no periodic progress messages

    try:
        await message.channel.send(f"Done. Removed <@&{role_id}> from {removed} member(s).")
    except Exception:
        pass
    log_action("role_remove_all_done", f"role={role_id}", f"removed={removed}")


async def handle_recache_show_cache(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """Admin-only: Clear and re-download SHOW_CACHE_PER_CAT images per cat from RecentPics.
    Scans RecentPics to get the cat list, wipes existing cached files per cat, and refills.
    """
    message: discord.Message = ctx["message"]
    author = ctx["author"]
    is_admin = int(getattr(author, 'id', 0)) in (getattr(settings, 'admin_ids', []) or []) or \
               getattr(getattr(author, 'guild_permissions', None), 'administrator', False)
    if not is_admin:
        log_action("recache_denied", f"user={author.id}", "unauthorized")
        return

    try:
        await message.channel.send("Starting recache of show-photo images…")
    except Exception:
        pass

    names: list[str] = []
    name_arg = str(args.get("name") or "").strip()
    try:
        sheet_id = getattr(settings, "sheet_vision_id", None)
        if not sheet_id:
            try:
                await message.channel.send("Sheet ID is not configured.")
            except Exception:
                pass
            log_action("recache_error", "sheet", "missing sheet_vision_id")
            return
        gc = sheets_client()
        ws = gc.open_by_key(str(sheet_id)).worksheet("RecentPics")
        rows = ws.get_all_values()
        if name_arg:
            if name_arg.lower() in {"all", "*", "photos", "cache", "profiles"}:
                names = []
            else:
                # Resolve one cat to actual FULL_NAME from CatDatabase if possible
                from ..services.catsheets import get_cat_profile
                prof = await get_cat_profile(name_arg)
                if isinstance(prof, dict) and prof.get("actual_name"):
                    names = [prof["actual_name"]]
                else:
                    # fallback: try to match first column loosely
                    q = name_arg.lower()
                    for r in rows[1:]:
                        full = (r[0] if r else '').strip()
                        if full and q in full.lower():
                            names = [full]
                            break
                    if not names:
                        names = [name_arg]
        else:
            for r in rows[1:]:
                full = (r[0] if r else '').strip()
                if full:
                    names.append(full)
    except Exception as e:
        log_action("recache_error", "sheet", str(e))
        try:
            await message.channel.send(f"Sheet error: {e}")
        except Exception:
            pass
        return

    # Fallback to profile cache if we failed to collect names from RecentPics
    if not names:
        try:
            from ..services import profile_cache as PC
            if PC.cached_count() == 0:
                await PC.refresh_async()
            names = PC.all_actual_names()
        except Exception as e:
            log_action("recache_error", "fallback_profiles", str(e))
            names = names or []

    # Wipe and refill per cat with gentle concurrency
    base = settings.show_cache_dir
    os.makedirs(base, exist_ok=True)
    sem = asyncio.Semaphore(max(1, settings.show_cache_warm_concurrency))
    total = 0

    # One-time wipe of cached files for selected cats only
    try:
        targets = set()
        # Build id set from names by parsing leading number
        import re as _re
        for nm in names:
            m = _re.match(r"\s*(\d+)[\.|\s]", nm or "")
            if m:
                targets.add(m.group(1).zfill(3))
        for sub in os.listdir(base):
            p = os.path.join(base, sub)
            if os.path.isdir(p) and ((not targets) or (sub in targets)):
                for fn in os.listdir(p):
                    if fn.lower().endswith(('.jpg', '.json')):
                        try:
                            os.remove(os.path.join(p, fn))
                        except Exception:
                            pass
    except Exception:
        pass

    # Deduplicate while preserving order
    seen_names = set()
    unique_names = []
    for nm in names:
        key = nm.strip()
        if not key:
            continue
        if key in seen_names:
            continue
        seen_names.add(key)
        unique_names.append(key)

    async def _one(nm: str):
        nonlocal total
        async with sem:
            try:
                await ensure_cat_cache(nm, settings.show_cache_per_cat)
                total += 1
            except Exception as e:
                log_action("recache_error", nm, str(e))
            await asyncio.sleep(0.2)

    from ..services import show_cache as _sc
    _sc.reset_recentpics_cache()

    await asyncio.gather(*[_one(n) for n in unique_names])
    try:
        _sc.rebuild_name_index()
    except Exception:
        pass
    try:
        await message.channel.send(f"Recache complete for {total} cat(s).")
    except Exception:
        pass


async def handle_recache_catabase(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """Admin-only: Refresh the Catabase cache and write the CSV snapshot.
    Also refreshes the dynamic alias map so new names resolve immediately.
    """
    message: discord.Message = ctx["message"]
    author = ctx["author"]
    is_admin = int(getattr(author, 'id', 0)) in (getattr(settings, 'admin_ids', []) or []) or \
               getattr(getattr(author, 'guild_permissions', None), 'administrator', False)
    if not is_admin:
        log_action("recache_catabase_denied", f"user={author.id}", "unauthorized")
        return
    try:
        await message.channel.send("Refreshing Catabase profiles and names…")
    except Exception:
        pass
    try:
        n = await PC.refresh_async()
        # Force-refresh dynamic aliases so router sees new names immediately
        try:
            ALIAS.refresh_aliases_now()
        except Exception:
            pass
        try:
            await message.channel.send(f"Catabase refreshed: {n} profiles.")
        except Exception:
            pass
    except Exception as e:
        try:
            await message.channel.send(f"Catabase refresh error: {e}")
        except Exception:
            pass
        log_action("recache_catabase_error", "", str(e))
