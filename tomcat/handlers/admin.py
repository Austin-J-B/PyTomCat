from __future__ import annotations
from tomcat.config import settings
from tomcat.utils.sender import safe_send
from tomcat.logger import log_action

async def handle_silent_mode(args, ctx):
    author = ctx["author"]
    if author.id not in settings.admin_ids:
        log_action("silent_mode_denied", f"user={author.id}", "unauthorized")
        return
    settings.silent_mode = not settings.silent_mode
    state = "ON" if settings.silent_mode else "OFF"
    log_action("silent_mode_toggle", f"user={author.id}", state)
    await safe_send(ctx["channel"], f"Silent mode {state}")
