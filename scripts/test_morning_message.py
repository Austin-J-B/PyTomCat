"""Morning message regression: who the 7:45 post says is feeding today.

For each station on the day's schedule the message names the scheduled feeder,
or the substitute who took their place, or "Needs Sub" if they asked and nobody
has. Getting it wrong either pings the wrong volunteer or hides a station that
still needs covering.

The lookup behind it used to scan the whole sub log once per station and feeder,
re-canonicalizing every record's stations on each pass, and the morning message
reads every month ever — so the cost grew for the life of the club.
_open_request_index does it once.

Run: python scripts/test_morning_message.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Sends this process's machine log to a scratch directory, so the test does
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

import tomcat.aliases as aliases

#Resolve station aliases once up front: the background refresh would otherwise
#swap the table mid-run.
aliases._do_dyn_alias_refresh()

from tomcat.handlers import feeding

#build_morning_message asks the clock what day it is, so the fixtures have to
#agree with it rather than pinning a date of their own.
TODAY = feeding._today_iso()

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def request(rid: str, requester: Any, *, station: Optional[str] = None,
            stations: Optional[List[str]] = None, dates=(TODAY,),
            status: str = "requested") -> Dict[str, Any]:
    record: Dict[str, Any] = {"id": rid, "parent_id": rid, "requester": requester,
                             "status": status, "dates": list(dates)}
    if stations is not None:
        record["stations"] = stations
    if station is not None:
        record["station"] = station
    return record


class Bot:
    """A bot that resolves no names, so lines show bare ids."""

    guilds: List[Any] = []

    def get_user(self, _uid):
        return None

    def get_guild(self, _gid):
        return None


def morning(subs: List[Dict[str, Any]], sched: Dict[str, List[Any]]) -> str:
    """Run build_morning_message against a fixed sub log and schedule."""
    real_load = feeding._load_sub_files
    real_read = feeding._read_schedule_for_weekday
    feeding._load_sub_files = lambda *a, **k: [("subs.jsonl", list(subs))]
    feeding._read_schedule_for_weekday = lambda *a, **k: dict(sched)
    try:
        message, _view = asyncio.run(feeding.build_morning_message(Bot()))
        return message
    finally:
        feeding._load_sub_files = real_load
        feeding._read_schedule_for_weekday = real_read


def main() -> int:
    print("=" * 70)
    print("morning message regression tests")
    print("=" * 70)

    print("\n[1] a sub record's stations resolve to canonical names")
    check("a list of stations", ["Lot 50"],
          feeding._canonical_stations({"stations": ["Lot 50"]}))
    #"west" is a station alias; the schedule speaks canonical names.
    check("an alias resolves", ["West Hall"],
          feeding._canonical_stations({"stations": ["west"]}))
    check("a single station field", ["Lot 50"],
          feeding._canonical_stations({"station": "Lot 50"}))
    check("the list wins over the single field", ["Lot 50"],
          feeding._canonical_stations({"stations": ["Lot 50"], "station": "HOP"}))
    check("duplicates collapse", ["West Hall"],
          feeding._canonical_stations({"stations": ["west", "West Hall"]}))
    check("no stations at all", [], feeding._canonical_stations({}))
    check("an empty list falls through to the single field", ["HOP"],
          feeding._canonical_stations({"stations": [], "station": "HOP"}))

    print("\n[2] the open request index is keyed by requester and station")
    index = feeding._open_request_index([
        request("s1", "111", station="Lot 50"),
        request("s2", "222", stations=["West Hall", "HOP"]),
    ], TODAY)
    check("one station", "s1", index.get(("111", "Lot 50")))
    check("each station of a multi-station request", ("s2", "s2"),
          (index.get(("222", "West Hall")), index.get(("222", "HOP"))))
    check("a requester who asked for nothing here", None, index.get(("333", "Lot 50")))
    check("the wrong station for a real requester", None, index.get(("111", "HOP")))

    print("\n[3] only open requests for today are indexed")
    index = feeding._open_request_index([
        request("s1", "111", station="Lot 50", dates=("2020-01-01",)),
        request("s2", "222", station="HOP", status="accepted"),
        request("s3", "333", station="West Hall"),
    ], TODAY)
    check("another day is not today's problem", None, index.get(("111", "Lot 50")))
    check("an accepted record is not an open request", None, index.get(("222", "HOP")))
    check("today's open request is there", "s3", index.get(("333", "West Hall")))

    print("\n[4] a numeric requester id matches the schedule's")
    #The schedule stores ints and the log has held both.
    index = feeding._open_request_index([request("s1", 111, station="Lot 50")], TODAY)
    check("stored as a number, looked up as a string", "s1", index.get(("111", "Lot 50")))

    print("\n[5] the first matching request wins")
    index = feeding._open_request_index([
        request("s1", "111", station="Lot 50"),
        request("s2", "111", station="Lot 50"),
    ], TODAY)
    check("earliest in the log", "s1", index.get(("111", "Lot 50")))

    print("\n[6] the message itself")
    sched = {"Lot 50": [111], "West Hall": [222], "HOP": []}
    message = morning([], sched)
    check("a scheduled feeder is named", True, "**Lot 50**: 111" in message)
    check("a station with nobody on it", True, "**HOP**: Unassigned" in message)
    check("no sub notice when nothing is open", False,
          "looking for a substitute" in message)

    message = morning([request("s1", "111", station="Lot 50")], sched)
    check("an open request shows as needing a sub", True,
          "**Lot 50**: Needs Sub (for 111)" in message)
    check("and the notice appears", True, "looking for a substitute" in message)
    check("the other stations are unaffected", True, "**West Hall**: 222" in message)

    message = morning([
        request("s1", "111", station="Lot 50"),
        {"id": "s1-a", "parent_id": "s1", "status": "accepted", "assignee": "999",
         "station": "Lot 50", "dates": [TODAY]},
    ], sched)
    check("a claimed request names the substitute", True, "**Lot 50**: 999" in message)
    check("and drops the notice", False, "looking for a substitute" in message)

    print("\n[7] an alias in the log still matches the schedule")
    message = morning([request("s1", "222", station="west")], {"West Hall": [222]})
    check("west resolves to West Hall", True,
          "**West Hall**: Needs Sub (for 222)" in message)

    print("\n[8] records that cannot apply are ignored")
    for label, subs in [
        ("a request naming no station", [request("s1", "111")]),
        ("a request for another day",
         [request("s1", "111", station="Lot 50", dates=("2020-01-01",))]),
        ("a request by somebody not scheduled",
         [request("s1", "999", station="Lot 50")]),
    ]:
        message = morning(subs, {"Lot 50": [111]})
        check(label, True, "**Lot 50**: 111" in message)

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
