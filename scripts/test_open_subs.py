"""Open sub requests regression: which shifts the volunteer UI offers.

The claim page shows three lists, and getting the split wrong either offers a
shift twice or hides one nobody has taken:

  available        upcoming, nobody has claimed it
  upcoming_filled  upcoming, somebody has
  past             the date has gone, claimed or not

A request covering several stations becomes one entry per station, because a
claim is per station — so one station of a request can be filled while another
is still open.

Run: python scripts/test_open_subs.py
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#tomcat.main refuses to import without a session secret, by design -- the UI
#must never sign cookies with a default. None of these tests issue one; they
#just have to get through the import.
os.environ.setdefault("UI_SESSION_SECRET", "tests-do-not-sign-cookies")

from tomcat import main as tomcat_main

TODAY = date(2026, 5, 15)
FUTURE = "2026-05-20"
PAST = "2026-01-01"

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def requested(rid: str, *, station: Optional[str] = None,
              stations: Optional[List[str]] = None, when: Optional[str] = FUTURE,
              requester: Any = "100", requester_name: str = "") -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "id": rid, "status": "requested", "dates": [when] if when else [],
        "requester": requester, "requester_name": requester_name,
    }
    if stations is not None:
        record["stations"] = stations
    if station is not None:
        record["station"] = station
    return record


def accepted(rid: str, *, station: str, when: str = FUTURE,
             assignee: Any = "200") -> Dict[str, Any]:
    return {
        "id": f"{rid}-accept", "parent_id": rid, "status": "accepted",
        "dates": [when], "station": station, "assignee": assignee,
    }


def bucket(*records: Dict[str, Any], names=None, assignees=None) -> Dict[str, List[dict]]:
    accepted_map, items, _req_ids, _asg_ids = tomcat_main._collect_sub_request_items(
        [("subs-2026-05.ndjson", list(records))]
    )
    return tomcat_main._bucket_sub_requests(
        items, accepted_map, today=TODAY,
        requester_names=names or {}, assignee_names=assignees or {},
    )


def stations_in(buckets: Dict[str, List[dict]], name: str) -> List[str]:
    return [item["station"] for item in buckets[name]]


def main() -> int:
    print("=" * 70)
    print("open sub requests regression tests")
    print("=" * 70)

    print("\n[1] the three lists")
    buckets = bucket(requested("r1", station="Lot 50"))
    check("an unclaimed future shift is available", ["Lot 50"], stations_in(buckets, "available"))
    buckets = bucket(requested("r1", station="Lot 50"), accepted("r1", station="Lot 50"))
    check("a claimed future shift is filled", ["Lot 50"],
          stations_in(buckets, "upcoming_filled"))
    check("and not also available", [], stations_in(buckets, "available"))
    buckets = bucket(requested("r1", station="Lot 50", when=PAST))
    check("an unclaimed past shift is past", ["Lot 50"], stations_in(buckets, "past"))
    buckets = bucket(requested("r1", station="Lot 50", when=PAST),
                     accepted("r1", station="Lot 50", when=PAST))
    check("a claimed past shift is also past", ["Lot 50"], stations_in(buckets, "past"))
    #Today has not gone yet, so it is still offered.
    buckets = bucket(requested("r1", station="Lot 50", when=TODAY.isoformat()))
    check("today is not past", ["Lot 50"], stations_in(buckets, "available"))

    print("\n[2] a request covering several stations splits per station")
    buckets = bucket(requested("r1", stations=["Lot 50", "West Hall"]))
    check("both offered", ["Lot 50", "West Hall"], stations_in(buckets, "available"))
    buckets = bucket(requested("r1", stations=["Lot 50", "West Hall"]),
                     accepted("r1", station="West Hall"))
    check("the claimed one moves", ["West Hall"], stations_in(buckets, "upcoming_filled"))
    check("the other stays open", ["Lot 50"], stations_in(buckets, "available"))

    print("\n[3] a request with no usable date is still offered")
    #Nothing to compare against today, so it cannot be past.
    check("no date", 1, len(bucket(requested("r1", station="Lot 50", when=None))["available"]))
    check("unparseable date", 1,
          len(bucket(requested("r1", station="Lot 50", when="whenever"))["available"]))

    print("\n[4] dates are anchored to noon for the browser")
    item = bucket(requested("r1", station="Lot 50"))["available"][0]
    #A bare date renders as the day before in any timezone west of UTC.
    check("a bare date gains a midday time", "2026-05-20T12:00:00", item["date"])
    check("the raw date is kept alongside", "2026-05-20", item["date_raw"])
    item = bucket(requested("r1", station="Lot 50", when="2026-05-20T09:00:00"))["available"][0]
    check("a date that already has a time is left alone", "2026-05-20T09:00:00", item["date"])

    print("\n[5] names are filled from the lookup, not from the record")
    item = bucket(requested("r1", station="Lot 50", requester="100"),
                  names={100: "Alice"})["available"][0]
    check("requester named", "Alice", item["requester_name"])
    check("requester id as a string", "100", item["requester_id"])
    #A name already on the record wins; the lookup is only for filling gaps.
    item = bucket(requested("r1", station="Lot 50", requester_name="Recorded"),
                  names={100: "Alice"})["available"][0]
    check("a recorded name is not overwritten", "Recorded", item["requester_name"])
    item = bucket(requested("r1", station="Lot 50"), accepted("r1", station="Lot 50"),
                  assignees={200: "Bob"})["upcoming_filled"][0]
    check("assignee named", "Bob", item["assignee_name"])
    check("assignee id as a string", "200", item["assignee_id"])

    print("\n[6] ids that are not numbers do not break the listing")
    item = bucket(requested("r1", station="Lot 50", requester="alice"),
                  names={100: "Alice"})["available"][0]
    check("a non-numeric requester gets no name", "", item["requester_name"])
    check("but is still listed", "alice", item["requester_id"])
    item = bucket(requested("r1", station="Lot 50"),
                  accepted("r1", station="Lot 50", assignee="bob"),
                  assignees={200: "Bob"})["upcoming_filled"][0]
    check("a non-numeric assignee gets no name", "", item["assignee_name"])

    print("\n[7] which ids need a name looked up")
    _map, _items, req_ids, asg_ids = tomcat_main._collect_sub_request_items(
        [("a.ndjson", [
            requested("r1", station="Lot 50", requester="100"),
            requested("r2", station="West Hall", requester="101", requester_name="Named"),
            requested("r3", station="HOP", requester="alice"),
            accepted("r1", station="Lot 50", assignee="200"),
            accepted("r2", station="West Hall", assignee="bob"),
        ])]
    )
    check("only unnamed numeric requesters", {100}, req_ids)
    check("only numeric assignees", {200}, asg_ids)

    print("\n[8] a malformed log does not hide the rest")
    class Exploding:
        """A row iterator that fails partway, like a torn ndjson line."""

        def __iter__(self):
            yield requested("r1", station="Lot 50")
            raise ValueError("truncated line")

    accepted_map, items, _r, _a = tomcat_main._collect_sub_request_items([
        ("broken.ndjson", Exploding()),
        ("good.ndjson", [requested("r2", station="West Hall")]),
    ])
    check("the good file still loads", ["Lot 50", "West Hall"],
          sorted(item["station"] for item in items))
    check("no claims invented", {}, accepted_map)

    print("\n[9] records with no status or stations are ignored")
    check("cancelled rows are skipped", {"available": [], "upcoming_filled": [], "past": []},
          bucket({"id": "r1", "status": "cancelled", "station": "Lot 50",
                  "dates": [FUTURE]}))
    check("a request naming no station yields nothing",
          {"available": [], "upcoming_filled": [], "past": []},
          bucket(requested("r1")))
    check("an empty log yields empty lists",
          {"available": [], "upcoming_filled": [], "past": []}, bucket())

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
