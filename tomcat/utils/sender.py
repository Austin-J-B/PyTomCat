from __future__ import annotations
from typing import Any
import discord

# Canonical signature Pylance will accept everywhere.
async def safe_send(ch: discord.abc.Messageable, text: str) -> None:
    # Minimal guard. Expand if you want markdown fallback, chunking, etc.
    await ch.send(text)
