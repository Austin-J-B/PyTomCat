"""Labeler queue regression: which photos land in which queue.

The three labeler queues are all derived from the same photo metadata table:

  detect    - no boxes at all, so the detector has not run
  classify  - boxes present, but at least one box has no cat name
  manual    - at least one box a labeler marked NeedsReview

Getting these wrong either hides work from volunteers or shows them the same
photo in two queues. The scans also decide what a claim means: a photo claimed
by somebody else must not appear in your queue, but your own claim must.

All three walk every row of a ~12,000 row table, which is why they take their
inputs as arguments and run in a thread rather than on the event loop.

Run: python scripts/test_labeler_queues.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat.handlers import labeler

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


WIDTH = max(labeler.COL_SERIAL, labeler.COL_BOX_COORDS,
            labeler.COL_BOX_CAT_IDS, labeler.COL_URL) + 1


def row(serial, boxes="", labels="", url="http://x/img.jpg", *, short=False) -> List[str]:
    cells = [""] * WIDTH
    cells[labeler.COL_SERIAL] = "" if serial is None else str(serial)
    cells[labeler.COL_BOX_COORDS] = boxes
    cells[labeler.COL_BOX_CAT_IDS] = labels
    cells[labeler.COL_URL] = url
    return cells[:2] if short else cells


HEADER = ["header"] * WIDTH

#A representative table. "0.5 0.5 0.2 0.2" stands in for a YOLO box.
BOX = "0.5 0.5 0.2 0.2"
TABLE = [
    HEADER,
    row(101),                                           #never detected
    row(102, boxes=BOX, labels="Microwave"),            #fully labeled
    row(103, boxes=f"{BOX}|{BOX}", labels="Microwave"), #second box unnamed
    row(104, boxes=f"{BOX}|{BOX}", labels="|"),         #both unnamed
    row(105, boxes=BOX, labels="NeedsReview"),          #flagged for review
    row(106, boxes=f"{BOX}|{BOX}", labels="Microwave|NeedsReview"),
    row(107, boxes="rejected"),                         #rejected outright
    row(108, boxes="   "),                              #whitespace is no boxes
    row(None, boxes=BOX),                               #no serial
    row(109, short=True),                               #truncated row
]


def serials(queue: List[Dict[str, Any]]) -> List[int]:
    return [item["serial"] for item in queue]


def main() -> int:
    print("=" * 70)
    print("labeler queue regression tests")
    print("=" * 70)

    no_claims: Dict[Tuple[str, int], Dict[str, Any]] = {}

    print("\n[1] detect: photos the detector has not seen")
    queue = labeler._parse_queue_detect_candidates(TABLE, no_claims, "me")
    check("empty and whitespace-only boxes only", [101, 108], serials(queue))
    check("carries the url", "http://x/img.jpg", queue[0]["url"])
    check("rejected is not offered again", False, 107 in serials(queue))
    check("a row with no serial is skipped", True, all(s is not None for s in serials(queue)))

    print("\n[2] classify: boxes present, a name missing")
    queue = labeler._parse_queue_classify_candidates(TABLE, no_claims, "me")
    check("partially and fully unnamed", [103, 104], serials(queue))
    check("a fully labeled photo is done", False, 102 in serials(queue))
    check("counts boxes and labels", (2, 1),
          (queue[0]["num_boxes"], queue[0]["num_labeled"]))
    check("an empty label does not count", (2, 0),
          (queue[1]["num_boxes"], queue[1]["num_labeled"]))
    #NeedsReview is a name as far as classify is concerned, so a flagged box
    #does not come back here asking to be named again. Manual owns those.
    check("a wholly flagged photo belongs to manual", False, 105 in serials(queue))
    check("a mixed named/flagged photo does too", False, 106 in serials(queue))

    print("\n[3] manual: boxes a labeler flagged")
    queue = labeler._parse_queue_manual_candidates(TABLE, no_claims, "me")
    check("only flagged photos", [105, 106], serials(queue))
    check("which box needs review", [0], queue[0]["review_indices"])
    check("and in a mixed photo", [1], queue[1]["review_indices"])
    check("review count", 1, queue[1]["num_review"])
    check("total boxes kept", 2, queue[1]["num_boxes"])

    print("\n[4] queues do not overlap")
    detect = set(serials(labeler._parse_queue_detect_candidates(TABLE, no_claims, "me")))
    classify = set(serials(labeler._parse_queue_classify_candidates(TABLE, no_claims, "me")))
    manual = set(serials(labeler._parse_queue_manual_candidates(TABLE, no_claims, "me")))
    check("detect and classify are disjoint", set(), detect & classify)
    check("detect and manual are disjoint", set(), detect & manual)
    check("classify and manual are disjoint", set(), classify & manual)

    print("\n[5] claims hide a photo from everyone but its claimant")
    mine = {("detect", 101): {"user_id": "me"}}
    theirs = {("detect", 101): {"user_id": "someone-else"}}
    check("my own claim still shows", True,
          101 in serials(labeler._parse_queue_detect_candidates(TABLE, mine, "me")))
    check("someone else's claim hides it", False,
          101 in serials(labeler._parse_queue_detect_candidates(TABLE, theirs, "me")))
    check("a claim on another queue does not hide it", True,
          101 in serials(labeler._parse_queue_detect_candidates(
              TABLE, {("classify", 101): {"user_id": "someone-else"}}, "me")))
    for name, scan, key in [
        ("classify", labeler._parse_queue_classify_candidates, "classify"),
        ("manual", labeler._parse_queue_manual_candidates, "manual"),
    ]:
        held = {(key, 106): {"user_id": "someone-else"}}
        check(f"{name} respects claims too", False,
              106 in serials(scan(TABLE, held, "me")))

    print("\n[6] queues come back in serial order")
    shuffled = [HEADER, row(300), row(100), row(200)]
    check("sorted ascending", [100, 200, 300],
          serials(labeler._parse_queue_detect_candidates(shuffled, no_claims, "me")))

    print("\n[7] an empty or header-only table yields nothing")
    for scan in (labeler._parse_queue_detect_candidates,
                 labeler._parse_queue_classify_candidates,
                 labeler._parse_queue_manual_candidates):
        check(f"{scan.__name__}: header only", [], scan([HEADER], no_claims, "me"))
        check(f"{scan.__name__}: no rows", [], scan([], no_claims, "me"))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
