"""Sub claim regression: who may take which shift, and only once.

Claiming a substitute shift is the one place a volunteer writes to somebody
else's record, so the checks matter:

  * a claim names a request id and nothing else is trusted — the station, date,
    requester and requester name all come from the stored request, so a client
    cannot turn its own strings into a Discord mention
  * a claim must name a station and date the request actually asked for
  * a shift already taken is a 409, including twice inside one submission
  * validation is all-or-nothing, so a bad later pick cannot leave an earlier
    one half-written to the log

Run: python scripts/test_sub_claims.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#Stubs whatever of the CV and Google stacks is not installed, and gives
#tomcat.main a session secret. Must come before any tomcat import.
import _test_support  # noqa: F401

from tomcat import main as tomcat_main

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def requested(rid: str, station: str, date: str, requester="100", name="Alice") -> Dict[str, Any]:
    return {
        "kind": "sub_request", "id": rid, "parent_id": rid,
        "station": station, "stations": [station], "dates": [date],
        "requester": requester, "requester_name": name, "status": "requested",
    }


def accepted(rid: str, station: str, date: str, assignee="200") -> Dict[str, Any]:
    return {
        "kind": "sub_accept", "id": f"{rid}-accept", "parent_id": rid,
        "station": station, "stations": [station], "dates": [date],
        "assignee": assignee, "status": "accepted",
    }


def files(*records: Dict[str, Any]):
    """One log file's worth of records, in the shape _load_sub_files yields."""
    return [("subs-2026-05.ndjson", list(records))]


def main() -> int:
    print("=" * 70)
    print("sub claim regression tests")
    print("=" * 70)

    print("\n[1] the index separates open requests from taken shifts")
    index, taken = tomcat_main._index_sub_records(files(
        requested("r1", "Lot 50", "2026-05-01"),
        requested("r2", "West Hall", "2026-05-02"),
        accepted("r2", "West Hall", "2026-05-02"),
    ))
    check("open requests keyed by id", ["r1", "r2"], sorted(index))
    check("taken shifts keyed by request, station and date",
          {("r2", "West Hall", "2026-05-02")}, taken)

    print("\n[2] a multi-station request marks each station taken separately")
    both = {
        "id": "r3", "parent_id": "r3", "stations": ["Lot 50", "West Hall"],
        "dates": ["2026-05-03"], "status": "accepted",
    }
    _index, taken = tomcat_main._index_sub_records(files(both))
    check("both stations recorded",
          {("r3", "Lot 50", "2026-05-03"), ("r3", "West Hall", "2026-05-03")}, taken)

    print("\n[3] a valid claim carries the stored request's details")
    index, taken = tomcat_main._index_sub_records(files(
        requested("r1", "Lot 50", "2026-05-01", requester="100", name="Alice"),
    ))
    picks = [{"id": "r1", "station": "Lot 50", "date": "2026-05-01"}]
    validated, error = tomcat_main._validate_sub_claims(picks, index, taken)
    check("no error", None, error)
    #The requester and their name come from the log, never from the pick.
    check("details read off the stored request",
          [("r1", "Lot 50", "2026-05-01", "100", "Alice")], validated)

    print("\n[4] a claim cannot invent what it is claiming")
    for label, pick, want in [
        ("an unknown request id", {"id": "nope", "station": "Lot 50", "date": "2026-05-01"},
         (404, "Substitute request not found")),
        ("a station the request did not ask for",
         {"id": "r1", "station": "West Hall", "date": "2026-05-01"},
         (400, "Claim does not match request")),
        ("a date the request did not ask for",
         {"id": "r1", "station": "Lot 50", "date": "2026-05-09"},
         (400, "Claim does not match request")),
        ("an unparseable date", {"id": "r1", "station": "Lot 50", "date": "someday"},
         (400, "Invalid date")),
        ("no date at all", {"id": "r1", "station": "Lot 50"}, (400, "Invalid date")),
        ("no station", {"id": "r1", "date": "2026-05-01"},
         (400, "Claim does not match request")),
        ("no id", {"station": "Lot 50", "date": "2026-05-01"},
         (404, "Substitute request not found")),
    ]:
        validated, error = tomcat_main._validate_sub_claims([pick], index, taken)
        check(label, ([], want), (validated, error))

    print("\n[5] a shift can only be taken once")
    index, taken = tomcat_main._index_sub_records(files(
        requested("r1", "Lot 50", "2026-05-01"),
        accepted("r1", "Lot 50", "2026-05-01"),
    ))
    validated, error = tomcat_main._validate_sub_claims(
        [{"id": "r1", "station": "Lot 50", "date": "2026-05-01"}], index, taken
    )
    check("an already-accepted shift is a conflict", (409, "Request already claimed"), error)

    index, taken = tomcat_main._index_sub_records(files(
        requested("r1", "Lot 50", "2026-05-01"),
    ))
    validated, error = tomcat_main._validate_sub_claims([
        {"id": "r1", "station": "Lot 50", "date": "2026-05-01"},
        {"id": "r1", "station": "Lot 50", "date": "2026-05-01"},
    ], index, taken)
    check("the same shift twice in one submission is too",
          (409, "Request already claimed"), error)

    print("\n[6] validation is all-or-nothing")
    index, taken = tomcat_main._index_sub_records(files(
        requested("r1", "Lot 50", "2026-05-01"),
        requested("r2", "West Hall", "2026-05-02"),
    ))
    #The first pick is perfectly valid; the second is not. Nothing comes back,
    #so the handler writes nothing.
    validated, error = tomcat_main._validate_sub_claims([
        {"id": "r1", "station": "Lot 50", "date": "2026-05-01"},
        {"id": "r2", "station": "Atlantis", "date": "2026-05-02"},
    ], index, taken)
    check("a bad later pick discards the good earlier one",
          ([], (400, "Claim does not match request")), (validated, error))
    #And two good picks both come through.
    validated, error = tomcat_main._validate_sub_claims([
        {"id": "r1", "station": "Lot 50", "date": "2026-05-01"},
        {"id": "r2", "station": "West Hall", "date": "2026-05-02"},
    ], index, taken)
    check("two good picks both validate", (2, None), (len(validated), error))

    print("\n[7] the submission itself has to be well formed")
    for label, picks, want in [
        ("not a list", {"id": "r1"}, (400, "Invalid picks")),
        ("a string", "r1", (400, "Invalid picks")),
        ("more than a month of picks",
         [{"id": "r1", "station": "Lot 50", "date": "2026-05-01"}] * 32,
         (400, "Invalid picks")),
        ("an entry that is not an object", ["r1"], (400, "Invalid claim")),
    ]:
        validated, error = tomcat_main._validate_sub_claims(picks, index, taken)
        check(label, ([], want), (validated, error))
    check("an empty list is not an error here", ([], None),
          tomcat_main._validate_sub_claims([], index, taken))

    print("\n[8] a claim uses the same identity rules as a submission")
    volunteer = {"user_id": "100", "username": "vol", "permissions": {"is_officer": False}}
    officer = {"user_id": "100", "username": "vol", "permissions": {"is_officer": True}}
    check("a volunteer cannot claim as someone else", "100",
          tomcat_main._acting_identity(volunteer, {"user_id": "999"})[0])
    check("an officer can claim for someone else", "999",
          tomcat_main._acting_identity(officer, {"user_id": "999"})[0])

    print("\n[9] the feeding channel gets one line for the whole batch")
    #A volunteer picking up four days of a request should not produce four pings.
    check("one shift",
          "<@200> picked up <@100>'s substitute request for Lot 50 on Wednesday, 05/20/2026",
          tomcat_main._claim_announcement("200", {"2026-05-20": [("Lot 50", "100", "Alice")]}))
    check("two stations on one date",
          "<@200> picked up <@100>'s substitute request for "
          "Lot 50 and HOP on Wednesday, 05/20/2026",
          tomcat_main._claim_announcement("200", {"2026-05-20": [
              ("Lot 50", "100", "Alice"), ("HOP", "100", "Alice")]}))
    check("three stations get the serial comma",
          "<@200> picked up <@100>'s substitute request for "
          "Lot 50, HOP, and West Hall on Wednesday, 05/20/2026",
          tomcat_main._claim_announcement("200", {"2026-05-20": [
              ("Lot 50", "100", "A"), ("HOP", "100", "A"), ("West Hall", "100", "A")]}))
    check("two dates are joined",
          "<@200> picked up <@100>'s substitute request for "
          "Lot 50 on Wednesday, 05/20/2026 and HOP on Thursday, 05/21/2026",
          tomcat_main._claim_announcement("200", {
              "2026-05-20": [("Lot 50", "100", "A")],
              "2026-05-21": [("HOP", "100", "A")]}))

    print("\n[10] a requester who cannot be mentioned is still named")
    check("a non-numeric id falls back to the name", "Alice",
          tomcat_main._requester_mention("alice", "Alice"))
    check("no id at all falls back to the name", "Bob",
          tomcat_main._requester_mention(None, "Bob"))
    check("neither leaves a readable stand-in", "someone",
          tomcat_main._requester_mention(None, ""))
    check("a numeric id becomes a mention", "<@100>",
          tomcat_main._requester_mention("100", "Alice"))

    print("\n[11] the line reads the same every run")
    #The requester list used to be a set, so with two requesters the order
    #changed between restarts.
    claims = {"2026-05-20": [("Lot 50", "100", "A"), ("HOP", "101", "B")]}
    first = tomcat_main._claim_announcement("200", claims)
    check("two requesters in the order they appear",
          "<@200> picked up <@100> and <@101>'s substitute request for "
          "Lot 50 and HOP on Wednesday, 05/20/2026", first)
    check("and stably", first, tomcat_main._claim_announcement("200", claims))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
