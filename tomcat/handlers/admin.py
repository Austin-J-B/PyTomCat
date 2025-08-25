from __future__ import annotations
import discord
from typing import Dict, Any
from ..config import settings
from ..logger import log_action

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
