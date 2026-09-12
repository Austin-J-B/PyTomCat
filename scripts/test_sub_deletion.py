"""Sub log deletion regression: who may remove which entry.

Deleting rewrites a shared log file, so two things matter. Permission: an
officer may remove anything, everyone else only entries they are party to — the
person who asked for the substitute, or the person who took it. And the rewrite
itself: only the named entry goes, nothing else in the file is disturbed, and
nothing is written at all if the request is refused.

Run: python scripts/test_sub_deletion.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat import main as tomcat_main

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def line(record: Dict[str, Any]) -> str:
    return json.dumps(record) + "\n"


REQUEST = {"id": "r1", "kind": "sub_request", "station": "Lot 50",
           "dates": ["2026-05-20"], "requester": "100", "status": "requested"}
CLAIM = {"id": "r1-accept", "kind": "sub_accept", "parent_id": "r1",
         "station": "Lot 50", "dates": ["2026-05-20"], "requester": "100",
         "assignee": "200", "status": "accepted"}
OTHER = {"id": "r9", "kind": "sub_request", "station": "HOP",
         "dates": ["2026-05-21"], "requester": "300", "status": "requested"}


def main() -> int:
    print("=" * 70)
    print("sub log deletion regression tests")
    print("=" * 70)

    print("\n[1] who may remove an entry")
    check("the requester may remove their own request", True,
          tomcat_main._may_delete_sub_record(REQUEST, "100", False))
    check("somebody else may not", False,
          tomcat_main._may_delete_sub_record(REQUEST, "999", False))
    check("an officer may remove anything", True,
          tomcat_main._may_delete_sub_record(REQUEST, "999", True))
    check("the assignee may remove a claim", True,
          tomcat_main._may_delete_sub_record(CLAIM, "200", False))
    check("so may the requester it belongs to", True,
          tomcat_main._may_delete_sub_record(CLAIM, "100", False))
    check("but nobody else", False,
          tomcat_main._may_delete_sub_record(CLAIM, "999", False))

    print("\n[2] an empty user id matches nobody")
    #Without this an entry recording no requester compared equal to a session
    #with no user id, and anyone unauthenticated could have removed it.
    check("empty id against an entry with no requester", False,
          tomcat_main._may_delete_sub_record({"id": "x"}, "", False))
    check("and against one that has one", False,
          tomcat_main._may_delete_sub_record(REQUEST, "", False))
    check("an officer with no id is still an officer", True,
          tomcat_main._may_delete_sub_record({"id": "x"}, "", True))

    print("\n[3] only the named entry is removed")
    removed, kept, forbidden = tomcat_main._remove_sub_record(
        [line(REQUEST), line(OTHER), line(CLAIM)], "r1",
        user_id="100", is_officer=False,
    )
    check("not refused", False, forbidden)
    check("the right entry came out", "r1", removed["id"])
    check("the others stay", ["r9", "r1-accept"],
          [json.loads(text)["id"] for text in kept])

    print("\n[4] a refused deletion writes nothing")
    removed, kept, forbidden = tomcat_main._remove_sub_record(
        [line(REQUEST), line(OTHER)], "r1", user_id="999", is_officer=False,
    )
    check("refused", True, forbidden)
    check("nothing removed", None, removed)
    #The handler writes `kept` back over the file, so it must be empty here
    #rather than a partial copy.
    check("and no replacement lines offered", [], kept)

    print("\n[5] an id that is not in the log removes nothing")
    removed, kept, forbidden = tomcat_main._remove_sub_record(
        [line(REQUEST), line(OTHER)], "nope", user_id="100", is_officer=False,
    )
    check("nothing removed", (None, False), (removed, forbidden))
    check("every line kept", 2, len(kept))

    print("\n[6] a line that will not parse is left alone")
    removed, kept, forbidden = tomcat_main._remove_sub_record(
        ["not json\n", line(REQUEST), "{truncated\n"], "r1",
        user_id="100", is_officer=False,
    )
    check("the target still goes", "r1", removed["id"])
    check("the unparseable lines survive verbatim", ["not json\n", "{truncated\n"], kept)

    print("\n[7] duplicate ids all go, and the last is reported")
    first = dict(REQUEST, station="Lot 50")
    second = dict(REQUEST, station="HOP")
    removed, kept, forbidden = tomcat_main._remove_sub_record(
        [line(first), line(second), line(OTHER)], "r1",
        user_id="100", is_officer=False,
    )
    check("only the unrelated line remains", 1, len(kept))
    check("the last match is reported", "HOP", removed["station"])

    print("\n[8] the announcement names what went")
    check("a request",
          "**Request Deleted**: <@100> removed the item for **Lot 50** on "
          + tomcat_main._format_date_for_notification("2026-05-20") + ".",
          tomcat_main._deletion_announcement(REQUEST, "2026-05-20", "<@100>"))
    check("a claim",
          "**Claim Deleted**: <@200> removed the item for **Lot 50** on "
          + tomcat_main._format_date_for_notification("2026-05-20") + ".",
          tomcat_main._deletion_announcement(CLAIM, "2026-05-20", "<@200>"))
    check("an entry with no station", True,
          "**Unknown**" in tomcat_main._deletion_announcement(
              {"kind": "sub_request"}, "2026-05-20", "<@100>"))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
