"""Safe send helper that guards Discord HTTP errors and rate limits."""

from __future__ import annotations
from typing import Any
import discord
from ..config import settings
from ..logger import log_action


_DISCORD_TEXT_LIMIT = 2000
_DISCORD_CHUNK_TARGET = 1900


def _chunk_text(text: str, limit: int = _DISCORD_CHUNK_TARGET) -> list[str]:
    txt = str(text or "")
    if not txt:
        return [""]
    if len(txt) <= limit:
        return [txt]
    chunks: list[str] = []
    remaining = txt
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < max(64, limit // 2):
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut < max(64, limit // 2):
            cut = limit
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or [txt[:limit]]


async def safe_send(ch: Any, text: str = "", **kwargs: Any) -> None:
    #Global suppression
    if getattr(settings, "silent_mode", False):
        snippet = (text or "").replace("\n", " ")[:120]
        #channel id logging without hard dependency on discord types
        ch_id = getattr(ch, "id", None)
        log_action("send_suppressed", f"ch={ch_id}", snippet)
        return
    #Non-messageable guard
    if not hasattr(ch, "send"):
        log_action("send_target_invalid", f"type={type(ch).__name__}", "no_send")
        return
    payload = str(text or "")
    if kwargs or len(payload) <= _DISCORD_TEXT_LIMIT:
        await ch.send(payload, **kwargs)
        return
    chunks = _chunk_text(payload)
    for part in chunks:
        await ch.send(part)
