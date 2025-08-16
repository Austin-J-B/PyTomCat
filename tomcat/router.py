from __future__ import annotations
from typing import Awaitable, Callable

Handler = Callable[[object, dict], Awaitable[None]]

class Router:
    def __init__(self):
        self._handlers: dict[str, Handler] = {}

    def register(self, intent_type: str, handler: Handler):
        self._handlers[intent_type] = handler

    async def dispatch(self, intent, ctx: dict):
        fn = self._handlers.get(intent.type)
        if fn:
            await fn(intent, ctx)