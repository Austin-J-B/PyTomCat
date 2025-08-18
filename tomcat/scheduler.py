from __future__ import annotations
import asyncio, datetime
from zoneinfo import ZoneInfo
from tomcat.config import settings
from tomcat.services.dues_ingest import poll_gmail_once

async def dues_ingest_loop():
    while True:
        try:
            if settings.gmail_enabled:
                poll_gmail_once()
        except Exception:
            pass
        await asyncio.sleep(180)
