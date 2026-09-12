"""Photo picker regression: which of a cat's photos gets sent.

"show me microwave" and "show me another" both come down to catsheets picking a
row out of the photo metadata and handing back a payload. They share the local-file
filter, the serial reader and the payload builder, so this test pins the newest-first
pick, the random pick's position in the history, the rows that must be skipped
because their file is gone, and the refresh-and-retry on an empty result.

Run: python scripts/test_photo_picker.py
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Sends this process's machine log to a scratch directory, so the test does
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

from tomcat.services import catsheets

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def entry(serial: Optional[int], url: str, *, label: str = "Microwave",
          box: int = 0, boxes: Optional[List[int]] = None) -> Dict:
    """One photo metadata row, with only the columns the picker reads."""
    row = [""] * (max(catsheets.COL_SERIAL, catsheets.COL_URL) + 1)
    row[catsheets.COL_SERIAL] = "" if serial is None else str(serial)
    row[catsheets.COL_URL] = url
    return {"row": row, "matched_label": label, "matched_box_index": box,
            "matched_box_indices": boxes}


class Stubs:
    """Serve fixed metadata rows and a fixed set of cached files."""

    def __init__(self, entries: List[Dict], local: Set[int]):
        self.entries = entries
        self.local = local
        self.refreshes = 0
        self.lookups = 0

    def install(self) -> None:
        def matched(_name):
            self.lookups += 1
            return list(self.entries)

        def refresh():
            self.refreshes += 1

        catsheets._matched_photo_entries = matched
        catsheets.force_refresh_photo_rows_cache = refresh
        catsheets.local_photos.has_local_photo = lambda serial: int(serial) in self.local
        catsheets._display_label = lambda name: str(name).title()


def main() -> int:
    print("=" * 70)
    print("photo picker regression tests")
    print("=" * 70)

    print("\n[1] the newest photo is the highest serial, not the last row")
    Stubs([entry(10, "u10"), entry(30, "u30"), entry(20, "u20")], {10, 20, 30}).install()
    result = asyncio.run(catsheets.get_most_recent_photo("microwave"))
    check("picks serial 30", "u30", result["url"])
    check("serial reported", "30", result["serial"])
    check("counts all candidates", 3, result["total_available"])
    check("display name titled", "Microwave", result["actual_name"])

    print("\n[2] rows whose file is gone are skipped, not counted")
    Stubs([entry(1, "u1"), entry(999, "u999"), entry(2, "u2")], {1, 2}).install()
    result = asyncio.run(catsheets.get_most_recent_photo("microwave"))
    check("the missing file is not picked", "u2", result["url"])
    check("and not counted", 2, result["total_available"])

    print("\n[3] rows with no serial are skipped")
    stubs = Stubs([entry(None, "ux"), entry(0, "uy")], {0})
    stubs.install()
    result = asyncio.run(catsheets.get_most_recent_photo("microwave"))
    check("no photos message", "No photos found for Microwave.", result)

    print("\n[4] an empty result refreshes the cache and retries once")
    stubs = Stubs([], set())
    stubs.install()
    result = asyncio.run(catsheets.get_most_recent_photo("microwave"))
    check("gives up with a message", "No photos found for Microwave.", result)
    check("refreshed exactly once", 1, stubs.refreshes)
    check("looked twice", 2, stubs.lookups)

    stubs = Stubs([], set())
    stubs.install()
    result = asyncio.run(catsheets.get_recent_photo("microwave"))
    check("random pick has its own message", "No recent photos for 'Microwave'.", result)
    check("also refreshed once", 1, stubs.refreshes)

    print("\n[5] the random pick reports where it falls in the history")
    Stubs([entry(10, "u10"), entry(30, "u30"), entry(20, "u20")], {10, 20, 30}).install()
    seen = {}
    for seed in range(30):
        random.seed(seed)
        result = asyncio.run(catsheets.get_recent_photo("microwave"))
        seen[result["url"]] = result["reverse_index"]
    check("every photo is reachable", {"u10", "u20", "u30"}, set(seen))
    #Oldest first: serial 10 is the first photo we have, 30 the third.
    check("index follows serial order", {"u10": 1, "u20": 2, "u30": 3}, seen)

    print("\n[6] the matched box information is carried through")
    Stubs([entry(5, "u5", label="Twix", box=2, boxes=[2, 3])], {5}).install()
    result = asyncio.run(catsheets.get_most_recent_photo("twix"))
    check("label", "Twix", result["matched_label"])
    check("box index", 2, result["matched_box_index"])
    check("box indices", [2, 3], result["matched_box_indices"])

    Stubs([entry(5, "u5", boxes=None)], {5}).install()
    result = asyncio.run(catsheets.get_most_recent_photo("microwave"))
    check("missing box indices become a list", [], result["matched_box_indices"])

    print("\n[7] a metadata failure is reported, not raised")
    def boom(_name):
        raise RuntimeError("sheet down")

    catsheets._matched_photo_entries = boom
    check("newest photo", "Photo metadata error: sheet down",
          asyncio.run(catsheets.get_most_recent_photo("microwave")))
    check("random photo", "Photo metadata error: sheet down",
          asyncio.run(catsheets.get_recent_photo("microwave")))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
