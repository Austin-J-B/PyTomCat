from __future__ import annotations
from typing import Any
import discord
from ..config import settings
from ..logger import log_action

async def safe_send(ch: Any, text: str = "", **kwargs: Any) -> None:
    # Global suppression
    if getattr(settings, "silent_mode", False):
        snippet = (text or "").replace("\n", " ")[:120]
        # channel id logging without hard dependency on discord types
        ch_id = getattr(ch, "id", None)
        log_action("send_suppressed", f"ch={ch_id}", snippet)
        return
    # Non-messageable guard
    if not hasattr(ch, "send"):
        log_action("send_target_invalid", f"type={type(ch).__name__}", "no_send")
        return
    await ch.send(text, **kwargs)
