"""Sub log cache: parsed records must not go stale, and must not be shared.

The volunteer claim page asks for every month of the sub log on each poll, and
parsing normalizes station names, dates and ids for every row -- so the cost
grew for the life of the club. Rows are now parsed once per file and re-read
when the file changes.

Two ways that can go wrong, and both would be silent:
  - a stale read, where a claim or a new request does not show up; and
  - a shared read, where one caller editing rows in place (the accept and
    update paths do) corrupts what every later caller sees.

Run: python scripts/test_sub_file_cache.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List

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

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def write(path: str, rows: List[dict]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def record(rid: str, station: str = "Lot 50", dates=("2026-09-12",)) -> dict:
    return {"id": rid, "requester": 111, "station": station, "status": "requested",
            "dates": list(dates)}


def main() -> int:
    print("=" * 70)
    print("sub log cache tests")
    print("=" * 70)
    feeding._SUB_FILE_CACHE.clear()

    tmp = tempfile.mkdtemp(prefix="subcache")
    path = os.path.join(tmp, "2026-09.jsonl")

    print("\n[1] parsing still normalizes every row")
    write(path, [{"id": "s1", "requester": 111, "assignee": "222",
                  "station": "west", "stations": ["west", "West Hall"],
                  "dates": ["2026-09-12", "2026-09-12"]}])
    rows = feeding._read_sub_file(path, "2026-09")
    check("the month key is filled in", "2026-09", rows[0].get("log_month"))
    check("a station alias resolves", "West Hall", rows[0].get("station"))
    check("duplicate stations collapse", ["West Hall"], rows[0].get("stations"))
    #Numeric ids become ints whichever way the log spelled them, so the
    #schedule and the log compare equal.
    check("ids are coerced to one type", (111, 222),
          (rows[0].get("requester"), rows[0].get("assignee")))

    print("\n[2] a second read is served from the cache")
    check("the file is cached", True, path in feeding._SUB_FILE_CACHE)
    again = feeding._read_sub_file(path, "2026-09")
    check("same records", rows, again)
    #Parse would have to run for a row that is not in the file to appear.
    feeding._SUB_FILE_CACHE[path] = (feeding._sub_file_stamp(path), [record("cached")])
    check("the cached rows are what comes back", "cached",
          feeding._read_sub_file(path, "2026-09")[0]["id"])

    print("\n[3] callers get their own copy")
    feeding._SUB_FILE_CACHE.clear()
    write(path, [record("s1")])
    mine = feeding._read_sub_file(path, "2026-09")
    mine[0]["status"] = "accepted"
    mine[0]["dates"].append("2026-09-13")
    theirs = feeding._read_sub_file(path, "2026-09")
    check("a field I changed", "requested", theirs[0]["status"])
    check("a list I appended to", ["2026-09-12"], theirs[0]["dates"])

    print("\n[4] a rewritten file is re-read")
    write(path, [record("s1"), record("s2", station="HOP")])
    feeding._forget_sub_file(path)
    check("both records", ["s1", "s2"],
          [row["id"] for row in feeding._read_sub_file(path, "2026-09")])

    print("\n[5] and re-read even without being told, because the stamp changed")
    write(path, [record("s3")])
    check("the new record", ["s3"],
          [row["id"] for row in feeding._read_sub_file(path, "2026-09")])

    print("\n[6] writing through the module's own writers invalidates")
    feeding._read_sub_file(path, "2026-09")
    feeding._write_sub_file(path, [record("s4"), record("s5")])
    check("_write_sub_file", ["s4", "s5"],
          [row["id"] for row in feeding._read_sub_file(path, "2026-09")])

    real_root = feeding.SUBS_ROOT
    feeding.SUBS_ROOT = Path(tmp) / "subs"
    try:
        appended = feeding._sub_log_path_from_key("2026-09")
        feeding._append_sub_record(record("a1"), "2026-09")
        check("a first append is visible", ["a1"],
              [row["id"] for row in feeding._read_sub_file(appended, "2026-09")])
        feeding._append_sub_record(record("a2"), "2026-09")
        check("_append_sub_record", ["a1", "a2"],
              [row["id"] for row in feeding._read_sub_file(appended, "2026-09")])
    finally:
        feeding.SUBS_ROOT = real_root

    print("\n[7] a missing file reads empty and is not cached")
    gone = os.path.join(tmp, "1999-01.jsonl")
    check("no records", [], feeding._read_sub_file(gone, "1999-01"))
    check("nothing cached", False, gone in feeding._SUB_FILE_CACHE)

    print("\n[8] naming a log path does not create anything")
    #The read paths ask for a path per month; creating folders there cost a
    #syscall per month per request, and every writer makes its own folder.
    feeding.SUBS_ROOT = Path(tmp) / "untouched"
    try:
        named = feeding._sub_log_path_from_key("2031-04")
        check("the path is right", os.path.join(tmp, "untouched", "2031", "2031-04.jsonl"),
              named)
        check("no folder appeared", False, os.path.exists(os.path.dirname(named)))
    finally:
        feeding.SUBS_ROOT = real_root

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
