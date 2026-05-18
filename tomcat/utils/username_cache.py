"""Process-wide cache for Discord user/member display names.

Discord.py's built-in User cache is best-effort — members of large guilds or
DM-only users miss it, falling back to fetch_user/fetch_member which each
cost an HTTP request on Discord's per-route rate-limit bucket. The same uid
resolved across multiple subsystems (photo sync, catabase sync, scheduled
jobs) burns through the bucket fast and produces `discord.http` 429 warnings.

This module keeps a single dict for the bot process's lifetime so each uid
is resolved at most once over the network. Concurrent lookups for the same
uid are serialized through a per-uid lock to avoid duplicate in-flight
requests during the boot burst.

Negative results (deleted users, unreachable IDs) are cached as "" so we
don't keep retrying. If you ever need to invalidate (rare — Discord display
names change infrequently), call clear_cache().
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

_DISPLAY_CACHE: dict[int, str] = {}
_LOCKS: dict[int, asyncio.Lock] = {}
_LOCKS_MUTEX = asyncio.Lock()


async def get_display_name(
    uid: Any,
    bot: Any,
    *,
    guild_id: Optional[int] = None,
) -> str:
    """Return the best display name for a Discord user id.

    Resolution order:
      1. cache hit (instant)
      2. guild.get_member(uid)         # in-memory, free
      3. bot.get_user(uid)             # in-memory, free
      4. guild.fetch_member(uid)       # network, rate-limited
      5. bot.fetch_user(uid)           # network, rate-limited

    Returns "" if none succeed. Empty result is cached so we don't retry.
    """
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return ""
    if uid_int in _DISPLAY_CACHE:
        return _DISPLAY_CACHE[uid_int]

    async with _LOCKS_MUTEX:
        if uid_int in _DISPLAY_CACHE:
            return _DISPLAY_CACHE[uid_int]
        lock = _LOCKS.get(uid_int)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[uid_int] = lock

    async with lock:
        if uid_int in _DISPLAY_CACHE:
            return _DISPLAY_CACHE[uid_int]

        display = ""
        try:
            guild = None
            if guild_id is not None:
                try:
                    guild = bot.get_guild(int(guild_id))
                except Exception:
                    guild = None

            if guild is not None:
                member = guild.get_member(uid_int)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid_int)
                    except Exception:
                        member = None
                if member is not None:
                    display = (
                        str(getattr(member, "display_name", None) or "").strip()
                        or str(getattr(member, "global_name", None) or "").strip()
                        or str(getattr(member, "name", None) or "").strip()
                    )

            if not display:
                user = None
                try:
                    user = bot.get_user(uid_int)
                except Exception:
                    user = None
                if user is None and hasattr(bot, "fetch_user"):
                    try:
                        user = await bot.fetch_user(uid_int)
                    except Exception:
                        user = None
                if user is not None:
                    display = (
                        str(getattr(user, "global_name", None) or "").strip()
                        or str(getattr(user, "name", None) or "").strip()
                    )
        except Exception:
            display = ""

        _DISPLAY_CACHE[uid_int] = display
        return display


def cached_display_name(uid: Any) -> str:
    """Sync peek at the cache. Returns "" on miss (does not fetch)."""
    try:
        return _DISPLAY_CACHE.get(int(uid), "")
    except (TypeError, ValueError):
        return ""


def clear_cache() -> None:
    """Drop all cached entries. Useful for tests; rarely needed in production."""
    _DISPLAY_CACHE.clear()
    _LOCKS.clear()
