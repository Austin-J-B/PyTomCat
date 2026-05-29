"""Safe send helper that guards Discord HTTP errors and rate limits."""

from __future__ import annotations
import asyncio
import time
from collections import deque
from typing import Any
import discord
from ..config import settings
from ..logger import log_action


_DISCORD_TEXT_LIMIT = 2000
_DISCORD_CHUNK_TARGET = 1900

# ---- Send-health telemetry (logs only; never user-visible) ----
#
# discord.py puts a 30s timeout on every HTTP request. When the single-core
# event loop gets starved, ch.send() raises asyncio.TimeoutError and the bot
# goes silent while still showing online (the gateway is a separate socket).
# That failure mode was invisible because the empty str(TimeoutError) logged as
# "". We now record every send failure with its type and, when failures cluster
# in a short window, emit a single loud `send_health_degraded` line so the
# starvation is obvious in the logs the next time it happens.
_SEND_FAIL_TIMES: deque[float] = deque(maxlen=200)
_SEND_HEALTH_WINDOW_SEC = 60.0
_SEND_HEALTH_BURST_THRESHOLD = 3
_SEND_HEALTH_LOG_COOLDOWN_SEC = 60.0
_send_health_last_logged: float = 0.0


def _note_send_failure(ch: Any, exc: BaseException) -> None:
    """Record a failed Discord send and log loudly if failures are clustering."""
    global _send_health_last_logged
    now = time.monotonic()
    ch_id = getattr(ch, "id", None)
    log_action("send_failure", f"ch={ch_id}", f"{type(exc).__name__}: {exc}")
    _SEND_FAIL_TIMES.append(now)
    recent = sum(1 for t in _SEND_FAIL_TIMES if now - t <= _SEND_HEALTH_WINDOW_SEC)
    if recent >= _SEND_HEALTH_BURST_THRESHOLD and (now - _send_health_last_logged) >= _SEND_HEALTH_LOG_COOLDOWN_SEC:
        _send_health_last_logged = now
        log_action(
            "send_health_degraded",
            f"failures_{int(_SEND_HEALTH_WINDOW_SEC)}s={recent}",
            "Discord sends are timing out/failing in a burst — likely event-loop "
            "starvation (bot may appear online but silent). Check CV/identify load "
            "and per-reaction work.",
        )


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
    # Record send failures for telemetry, then re-raise so callers' existing
    # error handling is unchanged. CancelledError is intentionally NOT counted
    # (it's normal task teardown, not a Discord failure).
    try:
        if kwargs or len(payload) <= _DISCORD_TEXT_LIMIT:
            await ch.send(payload, **kwargs)
            return
        chunks = _chunk_text(payload)
        for part in chunks:
            await ch.send(part)
    except asyncio.CancelledError:
        raise
    except BaseException as e:
        _note_send_failure(ch, e)
        raise
