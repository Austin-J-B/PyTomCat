"""Duplicate sub regression: one shift, one substitute, everywhere.

On 2026-11-07 a volunteer's sub request for Business went into the log twice,
eleven seconds apart. Two other volunteers each claimed one copy. The morning
roster named the first claimer and the 8pm unfed ping named the second, because
the two posts resolved the duplicate in opposite directions.

This covers the three layers of the fix:

  * the morning roster, the 8pm ping and the schedule lookup share one rule,
    matched per station, and the earliest claim wins
  * the server will not file a second request for a shift its requester has
    already asked about
  * the server will not let a shift be claimed twice through two request ids

Run: python scripts/test_duplicate_subs.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Stubs whatever of the CV and Google stacks is not installed, and gives
#tomcat.main a session secret. Must come before any tomcat import.
import _test_support  # noqa: F401

import tomcat.aliases as aliases

aliases._do_dyn_alias_refresh()

from tomcat import main as tomcat_main
from tomcat.handlers import feeding

FAILURES: List[str] = []

TODAY = feeding._today_iso()
INCIDENT_DATE = "2026-11-07"
CAMI = 1478229149758787715
FIRST_CLAIMER = 1408905977167741068
SECOND_CLAIMER = 1037772447509917717

#The four records as they sit in logs/subs/2026/2026-11.jsonl on prod.
INCIDENT = [
    {"kind": "sub_request", "id": "sub-1793979024852-1", "parent_id": "sub-1793979024852",
     "station": "Business", "stations": ["Business"], "dates": ["2026-11-07"],
     "requester": "1478229149758787715", "requester_name": "mmonse0", "assignee": None,
     "status": "requested", "created_at": "2026-11-06T15:30:24.852564",
     "trigger_phrase": "ui_sub_request"},
    {"kind": "sub_request", "id": "sub-1793979035990-1", "parent_id": "sub-1793979035990",
     "station": "Business", "stations": ["Business"], "dates": ["2026-11-07"],
     "requester": "1478229149758787715", "requester_name": "mmonse0", "assignee": None,
     "status": "requested", "created_at": "2026-11-06T15:30:35.990215",
     "trigger_phrase": "ui_sub_request"},
    {"kind": "sub_accept", "id": "sub-accept-1793986992897-1", "parent_id": "sub-1793979024852-1",
     "station": "Business", "stations": ["Business"], "dates": ["2026-11-07"],
     "requester": "1478229149758787715", "assignee": "1408905977167741068",
     "status": "accepted", "channel_id": 0, "message_id": 0,
     "created_at": "2026-11-06T17:43:12.894238", "log_month": "2026-11",
     "message_preview": "", "trigger_phrase": "ui_sub_claim", "requester_name": "mmonse0"},
    {"kind": "sub_accept", "id": "sub-accept-1793995932598-1", "parent_id": "sub-1793979035990-1",
     "station": "Business", "stations": ["Business"], "dates": ["2026-11-07"],
     "requester": "1478229149758787715", "assignee": "1037772447509917717",
     "status": "accepted", "channel_id": 0, "message_id": 0,
     "created_at": "2026-11-06T20:12:12.595000", "log_month": "2026-11",
     "message_preview": "", "trigger_phrase": "ui_sub_claim", "requester_name": "mmonse0"},
]


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def parsed(records: List[Dict[str, Any]], *, on: str = INCIDENT_DATE) -> List[Dict[str, Any]]:
    """Records as the bot sees them: written to a jsonl and read back.

    Going through _parse_sub_file matters: it turns string ids into ints, and
    an earlier diagnosis of this bug went wrong by skipping that step.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "subs.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                record = dict(record)
                record["dates"] = [on if d == INCIDENT_DATE else d for d in record["dates"]]
                f.write(json.dumps(record) + "\n")
        return feeding._parse_sub_file(path, on[:7])


class Bot:
    """A bot that resolves no names, so lines show bare ids."""

    guilds: List[Any] = []

    def get_user(self, _uid):
        return None

    def get_guild(self, _gid):
        return None


def with_log(subs: List[Dict[str, Any]], sched: Dict[str, List[Any]], run):
    real_load = feeding._load_sub_files
    real_read = feeding._read_schedule_for_weekday
    feeding._load_sub_files = lambda *a, **k: [("subs.jsonl", list(subs))]
    feeding._read_schedule_for_weekday = lambda *a, **k: dict(sched)
    try:
        return asyncio.run(run())
    finally:
        feeding._load_sub_files = real_load
        feeding._read_schedule_for_weekday = real_read


def morning(subs, sched) -> str:
    return with_log(subs, sched, lambda: feeding.build_morning_message(Bot()))[0]


def eight_pm(subs, sched, unfed) -> str:
    return with_log(subs, sched, lambda: feeding.build_8pm_lines(
        Bot(), unfed=unfed, sched=sched, mention=True))


def schedule(subs, sched, on: date) -> str:
    return with_log(subs, sched, lambda: feeding.build_schedule_for_date(Bot(), on))


def requested(rid, station, day, requester="100", stations=None):
    return {"kind": "sub_request", "id": rid, "parent_id": rid, "station": station,
            "stations": stations or [station], "dates": [day], "requester": requester,
            "requester_name": "Alice", "status": "requested"}


def accepted(rid, station, day, assignee):
    return {"kind": "sub_accept", "id": f"{rid}-{assignee}-accept", "parent_id": rid,
            "station": station, "stations": [station], "dates": [day],
            "assignee": assignee, "status": "accepted"}


def main() -> int:
    print("=" * 70)
    print("duplicate sub regression tests")
    print("=" * 70)

    business = feeding._canonical_station("Business") or "Business"
    sched = {business: [CAMI], "Lot 50": [222]}

    print("\n[1] the 2026-11-07 incident: one shift, two claims")
    subs = parsed(INCIDENT, on=TODAY)
    check("the resolver names the first claimer",
          FIRST_CLAIMER, feeding._sub_claims_for_date(subs, TODAY).get((str(CAMI), business)))
    message = morning(subs, sched)
    check("the morning roster names the first claimer", True,
          f"**{business}**: {FIRST_CLAIMER}" in message)
    message = eight_pm(subs, sched, [business])
    check("the 8pm ping names the first claimer", True,
          f"**{business}** → <@{FIRST_CLAIMER}>" in message)
    check("and does not ping the second", False, str(SECOND_CLAIMER) in message)
    subs_on_day = parsed(INCIDENT)
    message = schedule(subs_on_day, sched, date.fromisoformat(INCIDENT_DATE))
    check("the schedule lookup names the first claimer", True,
          f"**{business}** → {FIRST_CLAIMER}" in message)

    print("\n[2] a claim counts against the station it took, not the whole request")
    subs = parsed([
        requested("r1", "Lot 50, West Hall", INCIDENT_DATE, requester="222",
                  stations=["Lot 50", "West Hall"]),
        accepted("r1", "West Hall", INCIDENT_DATE, "900"),
    ], on=TODAY)
    message = morning(subs, {"Lot 50": [222], "West Hall": [222]})
    check("the claimed station shows the sub", True, "**West Hall**: 900" in message)
    check("the unclaimed station still needs one", True,
          "**Lot 50**: Needs Sub (for 222)" in message)
    message = eight_pm(subs, {"Lot 50": [222], "West Hall": [222]}, ["Lot 50", "West Hall"])
    check("8pm agrees on the claimed station", True, "**West Hall** → <@900>" in message)
    check("8pm keeps the requester on the unclaimed one", True,
          "**Lot 50** → <@222>" in message)

    print("\n[3] claims that do not apply are ignored")
    subs = parsed([accepted("gone", "Lot 50", INCIDENT_DATE, "900")], on=TODAY)
    check("a claim whose request was deleted", {}, feeding._sub_claims_for_date(subs, TODAY))
    subs = parsed([
        requested("r1", "Lot 50", INCIDENT_DATE, requester="222"),
        accepted("r1", "HOP", INCIDENT_DATE, "900"),
    ], on=TODAY)
    check("a claim on a station the request never asked for", {},
          feeding._sub_claims_for_date(subs, TODAY))

    print("\n[3b] a sub who asks for their own sub hands the shift on")
    chain = parsed([
        requested("rb", "Lot 50", INCIDENT_DATE, requester="111"),
        accepted("rb", "Lot 50", INCIDENT_DATE, "222"),
        requested("ra", "Lot 50", INCIDENT_DATE, requester="222"),
        accepted("ra", "Lot 50", INCIDENT_DATE, "333"),
    ], on=TODAY)
    check("the resolver follows the chain", 333,
          feeding._sub_claims_for_date(chain, TODAY).get(("111", "Lot 50")))
    check("the morning roster names the end of it", True,
          "**Lot 50**: 333" in morning(chain, {"Lot 50": [111]}))
    check("so does 8pm", True,
          "**Lot 50** → <@333>" in eight_pm(chain, {"Lot 50": [111]}, ["Lot 50"]))
    loop = parsed([
        requested("r1", "Lot 50", INCIDENT_DATE, requester="111"),
        accepted("r1", "Lot 50", INCIDENT_DATE, "222"),
        requested("r2", "Lot 50", INCIDENT_DATE, requester="222"),
        accepted("r2", "Lot 50", INCIDENT_DATE, "111"),
    ], on=TODAY)
    check("a loop in the chain ends rather than spinning", True,
          ("111", "Lot 50") in feeding._sub_claims_for_date(loop, TODAY))

    print("\n[3c] a legacy row accepted in place still counts")
    legacy = parsed([{"id": "sub-123", "status": "accepted", "requester": 111,
                      "assignee": 222, "station": "Lot 50", "dates": [INCIDENT_DATE]}])
    message = schedule(legacy, {"Lot 50": [111]}, date.fromisoformat(INCIDENT_DATE))
    check("the schedule lookup shows the sub", True,
          "**Lot 50** → 222 (covering 111)" in message)

    print("\n[3d] the open list shows a covered duplicate as filled")
    raw = parsed(INCIDENT[:3])
    accepted_map, items, _r, _a = tomcat_main._collect_sub_request_items([("subs.jsonl", raw)])
    buckets = tomcat_main._bucket_sub_requests(
        items, accepted_map, today=date(2026, 11, 1), requester_names={}, assignee_names={})
    check("neither copy is still open", [], [i["id"] for i in buckets["available"]])
    check("both name the first claimer", [str(FIRST_CLAIMER)] * 2,
          [i["assignee_id"] for i in buckets["upcoming_filled"]])

    print("\n[4] the server will not file the same request twice")
    files = [("subs.jsonl", parsed(INCIDENT[:1]))]
    kept, skipped = tomcat_main._drop_duplicate_requests(
        [{"date": INCIDENT_DATE, "stations": ["Business"]}], CAMI, files)
    check("a repeat of the incident request is dropped", [], kept)
    check("and reported as skipped",
          [{"date": INCIDENT_DATE, "stations": ["Business"]}], skipped)
    kept, skipped = tomcat_main._drop_duplicate_requests(
        [{"date": INCIDENT_DATE, "stations": ["Business", "Lot 50"]},
         {"date": "2026-11-08", "stations": ["Business"]}], CAMI, files)
    check("new stations and dates still go through",
          [{"date": INCIDENT_DATE, "stations": ["Lot 50"]},
           {"date": "2026-11-08", "stations": ["Business"]}], kept)
    kept, _ = tomcat_main._drop_duplicate_requests(
        [{"date": INCIDENT_DATE, "stations": ["Business"]}], SECOND_CLAIMER, files)
    check("somebody else may ask for the same station",
          [{"date": INCIDENT_DATE, "stations": ["Business"]}], kept)

    print("\n[5] the server will not let a shift be claimed twice")
    #The log as it stood after the first claim: two copies, one taken.
    index, taken = tomcat_main._index_sub_records([("subs.jsonl", parsed(INCIDENT[:3]))])
    validated, error = tomcat_main._validate_sub_claims(
        [{"id": "sub-1793979035990-1", "station": "Business", "date": INCIDENT_DATE}],
        index, taken)
    check("the second copy cannot be claimed", (409, "That shift is already covered"), error)
    check("and nothing is written", [], validated)

    index, taken = tomcat_main._index_sub_records([("subs.jsonl", parsed(INCIDENT[:2]))])
    validated, error = tomcat_main._validate_sub_claims(
        [{"id": "sub-1793979024852-1", "station": "Business", "date": INCIDENT_DATE},
         {"id": "sub-1793979035990-1", "station": "Business", "date": INCIDENT_DATE}],
        index, taken)
    check("both copies in one submission", (409, "That shift is already covered"), error)

    validated, error = tomcat_main._validate_sub_claims(
        [{"id": "sub-1793979035990-1", "station": "Business", "date": INCIDENT_DATE}],
        index, taken)
    check("either copy alone is fine while neither is taken", None, error)

    #A claim on a request that was then deleted no longer covers the shift.
    orphan = [r for r in parsed(INCIDENT[:3]) if r["id"] != "sub-1793979024852-1"]
    index, taken = tomcat_main._index_sub_records([("subs.jsonl", orphan)])
    validated, error = tomcat_main._validate_sub_claims(
        [{"id": "sub-1793979035990-1", "station": "Business", "date": INCIDENT_DATE}],
        index, taken)
    check("a claim on a deleted request does not block the shift", None, error)

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
