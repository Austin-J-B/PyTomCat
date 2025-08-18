from __future__ import annotations
from tomcat.config import settings

async def safe_send(channel, *args, **kwargs):
    if settings.silent_mode:
        return None
    return await channel.send(*args, **kwargs)
