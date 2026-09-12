"""Cat profile parsing regression: CatDatabase rows in, profiles out.

The live sheet read and the CSV snapshot share one parser
(profile_cache._profiles_from_rows) instead of two copies of the same
header-matching and row-unpacking. The sheet's column order and header
spellings have both changed over time, so this test pins how columns are
located, what a missing column produces, and that both entry points agree.

Run: python scripts/test_profile_cache.py
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat.services import profile_cache as pc

FAILURES: List[str] = []

FIELDS = [
    "actual_name", "image_url", "location", "physical_description", "behavior",
    "birthday_estimate", "tnrd", "tnr_date", "sex", "nicknames", "comments",
    "last_seen_date", "last_seen_time", "last_seen_by",
]


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


CANONICAL_HEADER = [
    "Full Legal Name", "Image URL", "Location", "Physical Description", "Behavior",
    "Birthday Estimate", "TNRd", "TNR Date", "Sex", "Common Nicknames", "Comments",
    "Last Seen Date", "Last Seen Time", "Last Seen By",
]


def main() -> int:
    print("=" * 70)
    print("cat profile parsing regression tests")
    print("=" * 70)

    print("\n[1] the canonical layout maps every column")
    rows = [CANONICAL_HEADER, [
        "1. Microwave", "http://x/1.jpg", "West Hall", "tuxedo", "friendly",
        "2019", "Yes", "2020-01-01", "M", "Mike, Micro", "none",
        "2026-09-01", "18:00", "aj",
    ]]
    profiles = pc._profiles_from_rows(rows)
    check("keyed by normalized name", ["microwave"], list(profiles))
    check("field order preserved", FIELDS, list(profiles["microwave"]))
    check("actual_name keeps the sheet's numbering", "1. Microwave",
          profiles["microwave"]["actual_name"])
    check("nicknames read through", "Mike, Micro", profiles["microwave"]["nicknames"])

    print("\n[2] columns are found by header, not position")
    rows = [
        ["Nicknames", "Full Name", "Notes", "Mystery", "Link of Most Recent Image", "Birthday"],
        ["Panini", "3. Paquini", "a note", "?", "http://x/3.jpg", "2018"],
    ]
    profiles = pc._profiles_from_rows(rows)
    check("alternate spellings resolve",
          {"nicknames": "Panini", "comments": "a note",
           "image_url": "http://x/3.jpg", "birthday_estimate": "2018"},
          {k: profiles["paquini"][k]
           for k in ("nicknames", "comments", "image_url", "birthday_estimate")})
    check("columns with no header read as None", None, profiles["paquini"]["location"])

    print("\n[3] an unrecognizable name header falls back to the first column")
    profiles = pc._profiles_from_rows([["Whatever", "Location"], ["5. Eraser", "Lot 50"]])
    check("first column used as the name", ["eraser"], list(profiles))
    check("other columns still map", "Lot 50", profiles["eraser"]["location"])

    print("\n[4] short and empty rows")
    profiles = pc._profiles_from_rows([
        ["Full Name", "Image URL", "Location", "Sex"],
        ["7. Alaska"],
        ["8. Laufey", "http://x/8.jpg"],
        ["", "orphan", "", ""],
        ["   ", "blank name", "", ""],
    ])
    check("rows without a name are dropped", ["alaska", "laufey"], list(profiles))
    check("missing trailing cells read as None",
          [None, None, None],
          [profiles["alaska"][k] for k in ("image_url", "location", "sex")])
    check("present cells still read", "http://x/8.jpg", profiles["laufey"]["image_url"])

    print("\n[5] nothing to parse")
    check("no rows", {}, pc._profiles_from_rows([]))
    check("header only", {}, pc._profiles_from_rows([CANONICAL_HEADER]))
    check("one empty line", {}, pc._profiles_from_rows([[]]))

    print("\n[6] the CSV snapshot goes through the same parser")
    tmp = Path(tempfile.mkdtemp(prefix="tomcat-profiles-"))
    good = tmp / "cat.csv"
    with good.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CANONICAL_HEADER)
        writer.writerow(["2. Twix", "http://x/2.jpg", "HOP", "tabby", "shy",
                         "2021", "No", "", "F", "", "", "", "", ""])
    pc._readable_catabase_csv_paths = lambda: [str(tmp / "missing.csv"), str(good)]
    pc._CACHE, pc._TS = {}, 0.0
    pc._load_from_csv()
    check("a missing path is skipped for the next one", ["twix"], list(pc._CACHE))
    check("CSV matches the sheet parser",
          pc._profiles_from_rows([CANONICAL_HEADER,
                                  ["2. Twix", "http://x/2.jpg", "HOP", "tabby", "shy",
                                   "2021", "No", "", "F", "", "", "", "", ""]]),
          pc._CACHE)
    check("timestamp set", True, pc._TS > 0)

    print("\n[7] a sheet refresh populates the cache and its snapshots")
    workdir = tempfile.mkdtemp(prefix="tomcat-profiles-run-")
    os.chdir(workdir)
    sheet_rows = [CANONICAL_HEADER,
                  ["1. Microwave"] + [""] * 13,
                  ["2. Twix"] + [""] * 13]

    class FakeWorksheet:
        def get_all_values(self):
            return sheet_rows

    class FakeBook:
        def worksheet(self, _name):
            return FakeWorksheet()

    class FakeClient:
        def open_by_key(self, _key):
            return FakeBook()

    pc.sheets_client = lambda: FakeClient()
    pc.settings.sheet_catabase_id = "fake-sheet"
    pc._sync_metadata_names_from_catabase_rows = lambda rows: {}
    pc._CACHE, pc._TS, pc._COUNT = {}, 0.0, 0
    count = pc.refresh_sync()
    check("returns the profile count", 2, count)
    check("cache holds both cats", ["microwave", "twix"], sorted(pc._CACHE))
    check("json snapshot written", True, Path(pc._snapshot_path()).exists())
    check("csv snapshot written", True, Path(pc._preferred_catabase_csv_path()).exists())
    with open(pc._preferred_catabase_csv_path(), encoding="utf-8", newline="") as handle:
        check("csv snapshot keeps every column", sheet_rows, list(csv.reader(handle)))

    print("\n[8] a sheet with no usable rows leaves the cache alone")
    sheet_rows = [CANONICAL_HEADER]
    pc._CACHE = {"kept": {"actual_name": "kept"}}
    check("header-only sheet returns 0", 0, pc.refresh_sync())
    check("cache untouched", {"kept": {"actual_name": "kept"}}, pc._CACHE)
    pc.settings.sheet_catabase_id = None
    check("no sheet id configured returns 0", 0, pc.refresh_sync())

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
