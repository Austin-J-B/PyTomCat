"""Handlers for station-level informational queries."""
from __future__ import annotations

from typing import Any, Dict

from ..logger import log_action
from ..services.station_residents import get_residents_for_station
from ..utils.sender import safe_send


async def handle_station_residents(intent, ctx: Dict[str, Any]) -> None:
    """Respond with cats associated with a given feeding station."""
    channel = ctx["channel"]
    station = (intent.data or {}).get("station")
    raw_query = (intent.data or {}).get("query")

    if not station:
        if raw_query:
            await safe_send(channel, f"I couldn't match **{raw_query}** to a station.")
        else:
            await safe_send(channel, "I couldn't match that to a station.")
        log_action("station_residents", "unmatched", raw_query or "")
        return

    residents = get_residents_for_station(station)
    if not residents:
        await safe_send(channel, f"I don't have any residents recorded for **{station}** yet.")
        log_action("station_residents", f"station={station}", "no_residents")
        return

    names = ", ".join(residents)
    await safe_send(channel, f"Cats near **{station}**: {names}")
    log_action("station_residents", f"station={station}", f"count={len(residents)}")
