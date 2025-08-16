from __future__ import annotations
import re, random
import discord
from typing import Any

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
        if rx.search(content):
            if not _cool(message.author.id, now_ts):
                return
            resp = fn()
            await message.channel.send(resp)
            return  # stop after first match
        

