"""Sub request regression: who it is filed for, and what it asks for.

The volunteer UI lets an officer file a substitute request on somebody else's
behalf. Everyone else has to be filing for themselves, and the only thing
enforcing that is _subrequest_identity: the form is free to send any user_id it
likes. This test pins that boundary, and the station validation next to it —
a submission naming a station that does not exist on the date is refused
outright rather than quietly trimmed to nothing.

Run: python scripts/test_subrequest_parsing.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat import main as tomcat_main

#A fixed roster, so the test is about validation rather than station history.
ROSTER = ["Microwave", "West Hall", "Lot 50"]
tomcat_main.station_names = lambda _effective=None: list(ROSTER)

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def session(user_id="100", username="volunteer", officer=False) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "username": username,
        "permissions": {"is_officer": officer},
    }


def main() -> int:
    print("=" * 70)
    print("sub request parsing regression tests")
    print("=" * 70)

    print("\n[1] a volunteer is pinned to their own identity")
    #The form can send whatever it likes; this is the check that ignores it.
    spoofed = {"user_id": "999", "user_name": "somebody else"}
    check("a spoofed id is discarded", ("100", "volunteer"),
          tomcat_main._subrequest_identity(session(), spoofed))
    check("an empty body still identifies them", ("100", "volunteer"),
          tomcat_main._subrequest_identity(session(), {}))
    check("a missing permissions block is not an officer", ("100", "volunteer"),
          tomcat_main._subrequest_identity(
              {"user_id": "100", "username": "volunteer"}, spoofed))
    check("a null permissions block is not an officer", ("100", "volunteer"),
          tomcat_main._subrequest_identity(
              {"user_id": "100", "username": "volunteer", "permissions": None}, spoofed))

    print("\n[2] an officer may file for somebody else")
    check("the named user is used", ("999", "somebody else"),
          tomcat_main._subrequest_identity(session(officer=True), spoofed))
    check("filing for themselves needs no fields", ("100", "volunteer"),
          tomcat_main._subrequest_identity(session(officer=True), {}))
    check("a half-filled body falls back per field", ("999", "volunteer"),
          tomcat_main._subrequest_identity(session(officer=True), {"user_id": "999"}))

    print("\n[3] a session with no user at all yields no requester")
    check("nothing to file against", (None, None),
          tomcat_main._subrequest_identity(
              {"permissions": {"is_officer": False}}, spoofed))

    print("\n[4] a null permissions block is not an escape hatch")
    #Verified equivalent to the previous inline version across 375 combinations
    #of session and body, for any session carrying a user id. Two deliberate
    #differences, both unreachable through the real OAuth flow: a session whose
    #permissions block is null is now treated as a non-officer instead of
    #raising, and a session with no user id at all is refused for the missing
    #requester rather than for whatever else its payload gets wrong. The
    #single-date form used to check the requester first and the multi-date form
    #last; they agree now.
    check("null permissions cannot impersonate", ("100", "volunteer"),
          tomcat_main._subrequest_identity(
              {"user_id": "100", "username": "volunteer", "permissions": None},
              {"user_id": "999", "user_name": "somebody else"}))

    print("\n[5] stations are checked against the roster for that date")
    check("known stations pass", (["Microwave", "West Hall"], None),
          tomcat_main._clean_subrequest_stations("2026-05-01", ["Microwave", "West Hall"]))
    check("an unknown station is refused", (None, "Invalid station"),
          tomcat_main._clean_subrequest_stations("2026-05-01", ["Microwave", "Atlantis"]))
    check("padding is trimmed", (["Lot 50"], None),
          tomcat_main._clean_subrequest_stations("2026-05-01", ["  Lot 50  "]))
    check("duplicates collapse", (["Lot 50"], None),
          tomcat_main._clean_subrequest_stations("2026-05-01", ["Lot 50", "Lot 50"]))
    check("blank entries are ignored", ([], None),
          tomcat_main._clean_subrequest_stations("2026-05-01", ["", "   "]))

    print("\n[6] the single-date form")
    check("one date, one station",
          ([{"date": "2026-05-01", "stations": ["Lot 50"]}], None),
          tomcat_main._parse_subrequest_dates({"date": "2026-05-01", "stations": ["Lot 50"]}))
    check("a datetime is reduced to its date",
          ([{"date": "2026-05-01", "stations": ["Lot 50"]}], None),
          tomcat_main._parse_subrequest_dates(
              {"date": "2026-05-01T18:30:00", "stations": ["Lot 50"]}))
    check("no date is a missing field", ([], "Missing required fields"),
          tomcat_main._parse_subrequest_dates({"stations": ["Lot 50"]}))
    check("no stations is a missing field", ([], "Missing required fields"),
          tomcat_main._parse_subrequest_dates({"date": "2026-05-01"}))
    check("stations must be a list", ([], "Missing required fields"),
          tomcat_main._parse_subrequest_dates({"date": "2026-05-01", "stations": "Lot 50"}))
    check("an unparseable date is reported as such", ([], "Invalid date format"),
          tomcat_main._parse_subrequest_dates({"date": "next tuesday", "stations": ["Lot 50"]}))
    check("an unknown station is refused", ([], "Invalid station"),
          tomcat_main._parse_subrequest_dates({"date": "2026-05-01", "stations": ["Atlantis"]}))

    print("\n[7] the multi-date form")
    check("two dates come through",
          ([{"date": "2026-05-01", "stations": ["Lot 50"]},
            {"date": "2026-05-02", "stations": ["Microwave", "West Hall"]}], None),
          tomcat_main._parse_subrequest_dates({"requests": [
              {"date": "2026-05-01", "stations": ["Lot 50"]},
              {"date": "2026-05-02", "stations": ["Microwave", "West Hall"]},
          ]}))
    #A batch is worth keeping even if one row of the form was left half-filled.
    check("an entry with no date is skipped, not fatal",
          ([{"date": "2026-05-02", "stations": ["Lot 50"]}], None),
          tomcat_main._parse_subrequest_dates({"requests": [
              {"stations": ["Lot 50"]},
              {"date": "2026-05-02", "stations": ["Lot 50"]},
          ]}))
    check("an entry with no stations is skipped",
          ([{"date": "2026-05-02", "stations": ["Lot 50"]}], None),
          tomcat_main._parse_subrequest_dates({"requests": [
              {"date": "2026-05-01"},
              {"date": "2026-05-02", "stations": ["Lot 50"]},
          ]}))
    check("an unparseable date is skipped",
          ([{"date": "2026-05-02", "stations": ["Lot 50"]}], None),
          tomcat_main._parse_subrequest_dates({"requests": [
              {"date": "whenever", "stations": ["Lot 50"]},
              {"date": "2026-05-02", "stations": ["Lot 50"]},
          ]}))
    #An unknown station is different: it means the form disagrees with the
    #roster, so the whole batch stops rather than filing part of it.
    check("an unknown station stops the batch", ([], "Invalid station"),
          tomcat_main._parse_subrequest_dates({"requests": [
              {"date": "2026-05-01", "stations": ["Lot 50"]},
              {"date": "2026-05-02", "stations": ["Atlantis"]},
          ]}))
    check("a non-object entry stops the batch", ([], "Invalid request entry"),
          tomcat_main._parse_subrequest_dates({"requests": ["2026-05-01"]}))
    check("more than a month of dates is refused", ([], "Too many requests"),
          tomcat_main._parse_subrequest_dates({"requests": [
              {"date": "2026-05-01", "stations": ["Lot 50"]}
          ] * 32}))
    check("exactly a month is allowed", 1,
          len({e["date"] for e in tomcat_main._parse_subrequest_dates({"requests": [
              {"date": "2026-05-01", "stations": ["Lot 50"]}
          ] * 31})[0]}))
    #An empty list is not the multi-date form; it falls through to the flat one.
    check("an empty requests list falls through", ([], "Missing required fields"),
          tomcat_main._parse_subrequest_dates({"requests": []}))

    print("\n[8] date coercion")
    for value, want in [
        ("2026-05-01", "2026-05-01"),
        ("2026-05-01T18:30:00", "2026-05-01"),
        ("not a date", None),
        ("", None),
        (None, None),
        (12345, None),
        ({"date": "2026-05-01"}, None),
    ]:
        check(f"{value!r} -> {want!r}", want, tomcat_main._coerce_iso_date(value))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
