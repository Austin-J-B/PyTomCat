from __future__ import annotations
import discord
from typing import Dict, Any
from ..config import settings
from ..logger import log_action

async def handle_silent_mode(args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    author = ctx["author"]
    ch = ctx["channel"]

    # Gate by admin IDs parsed as ints in config.py
    if int(author.id) not in settings.admin_ids:
        log_action("silent_mode_denied", f"user={author.id}", "unauthorized")
        return  # no message; stay truly silent for non-admins

    # Flip and report
    settings.silent_mode = not bool(settings.silent_mode)
    state = "enabled" if settings.silent_mode else "disabled"
    log_action("silent_mode_toggle", f"user={author.id}", state)
    await ch.send(f"Silent mode {state}.")
