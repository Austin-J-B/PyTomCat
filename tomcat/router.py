from __future__ import annotations
from typing import Awaitable, Callable
from .intents import Intent
from .logger import log_action

Handler = Callable[[Intent, dict], Awaitable[None]]

class Router:
    def __init__(self):
        self._handlers: dict[str, Handler] = {}

    def register(self, intent_type: str, handler: Handler) -> None:
        self._handlers[intent_type] = handler

    async def dispatch(self, intent: Intent, ctx: dict) -> None:
        fn = self._handlers.get(intent.type)
        if not fn:
            log_action("dispatch_missing", f"intent={intent.type}", "no handler")
            return
        log_action("dispatch", f"intent={intent.type}", f"handler={fn.__name__}")
        await fn(intent, ctx)
        log_action("handled", f"intent={intent.type}", "ok")
