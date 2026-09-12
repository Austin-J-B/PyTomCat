"""Feeding-schedule store regression: versions in, the right week out.

tomcat/services/schedule_store.py replaced two independent copies of this logic
(one in the web server, one in the bot) and put a cache in front of the file.
This test pins the version-picking rules, the legacy migration, and that a save
is visible to the very next read.

Run: python scripts/test_schedule_store.py
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Sends this process's machine log to a scratch directory, so the test does
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def fresh_store(tmp: Path):
    """A store module rooted at a throwaway cache directory."""
    from tomcat.services import schedule_store as store
    importlib.reload(store)
    store.SCHEDULE_PATH = tmp / "feeding_schedule.ndjson"
    store.LEGACY_SCHEDULE_PATH = tmp / "feeding_schedule.json"
    store._CACHE = None
    store._CACHE_STAMP = None
    store._CACHE_CHECKED_MONO = float("-inf")
    #Stations are filtered against the definitions file, which this test is not
    #about; accept every name instead.
    store.station_names = lambda _effective=None: [
        "Alpha", "Beta", "Gamma",
    ]
    return store


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tomcat-schedule-"))
    store = fresh_store(tmp)

    print("=" * 70)
    print("feeding schedule store regression tests")
    print("=" * 70)

    print("\n[1] an empty store resolves to nothing, not an error")
    check("no versions", [], store.load_versions())
    check("empty resolve",
          {"schedule": {}, "effective_from": store.DEFAULT_EFFECTIVE, "meta": {}},
          store.resolve_for_date(date(2026, 5, 1)))
    check("no version for date", None, store.version_for_date(date(2026, 5, 1)))

    print("\n[2] the latest version effective on or before the date wins")
    store.save_versions([
        {"effective_from": "2026-01-01", "schedule": {"Alpha": ["a"]}, "meta": {"n": 1}},
        {"effective_from": "2026-03-01", "schedule": {"Alpha": ["b"]}, "meta": {"n": 2}},
        {"effective_from": "2026-02-01", "schedule": {"Alpha": ["c"]}, "meta": {"n": 3}},
    ])
    for when, want in [
        (date(2026, 1, 1), "2026-01-01"),
        (date(2026, 1, 31), "2026-01-01"),
        (date(2026, 2, 1), "2026-02-01"),
        (date(2026, 2, 28), "2026-02-01"),
        (date(2026, 3, 1), "2026-03-01"),
        (date(2099, 1, 1), "2026-03-01"),
    ]:
        check(f"{when} -> {want}", want, store.resolve_for_date(when)["effective_from"])
    print("  (a date before every version falls back to the earliest)")
    check("1969 falls back to earliest", "2026-01-01",
          store.resolve_for_date(date(1969, 1, 1))["effective_from"])
    check("meta comes along", {"n": 3}, store.resolve_for_date(date(2026, 2, 15))["meta"])

    print("\n[3] the schedule is limited to that week's known stations")
    store.save_versions([
        {"effective_from": "2026-01-01",
         "schedule": {"Alpha": ["a"], "Retired Station": ["x"], "Beta": ["b"]},
         "meta": {}},
    ])
    check("unknown stations dropped", {"Alpha": ["a"], "Beta": ["b"]},
          store.resolve_for_date(date(2026, 6, 1))["schedule"])

    print("\n[4] a save is visible to the next read")
    store.upsert_version({"Gamma": ["g"]}, "2026-04-01", {"source": "test"})
    check("new version readable immediately", {"Gamma": ["g"]},
          store.resolve_for_date(date(2026, 4, 2))["schedule"])
    check("upsert replaces the same effective_from", 2, len(store.load_versions()))
    store.upsert_version({"Gamma": ["g2"]}, "2026-04-01")
    check("still two versions after replace", 2, len(store.load_versions()))
    check("replacement took effect", {"Gamma": ["g2"]},
          store.resolve_for_date(date(2026, 4, 2))["schedule"])
    check("existing meta preserved on replace", {"source": "test"},
          store.resolve_for_date(date(2026, 4, 2))["meta"])

    print("\n[5] unparseable and unstamped lines are skipped, not fatal")
    store.SCHEDULE_PATH.write_text(
        "not json\n"
        '{"no_effective_from": true}\n'
        '{"effective_from": "2026-01-01", "schedule": {"Alpha": ["a"]}}\n'
        '{"effective_from": "not-a-date", "schedule": {"Beta": ["b"]}}\n'
        '{"meta": {"updated_at": 1}}\n',
        encoding="utf-8",
    )
    store._CACHE = None
    store._CACHE_CHECKED_MONO = float("-inf")
    check("two stamped versions kept", 2, len(store.load_versions()))
    check("undated version ignored when picking", "2026-01-01",
          store.resolve_for_date(date(2026, 6, 1))["effective_from"])

    print("\n[6] the legacy single-schedule file migrates forward once")
    tmp2 = Path(tempfile.mkdtemp(prefix="tomcat-schedule-legacy-"))
    store = fresh_store(tmp2)
    store.LEGACY_SCHEDULE_PATH.write_text(
        json.dumps({"schedule": {"Alpha": ["a"]}, "meta": {"source": "legacy"}}),
        encoding="utf-8",
    )
    check("legacy schedule read", {"Alpha": ["a"]},
          store.resolve_for_date(date(2026, 6, 1))["schedule"])
    check("migrated to ndjson", True, store.SCHEDULE_PATH.exists())
    store._CACHE = None
    store._CACHE_CHECKED_MONO = float("-inf")
    check("ndjson now the source", {"Alpha": ["a"]},
          store.resolve_for_date(date(2026, 6, 1))["schedule"])

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
