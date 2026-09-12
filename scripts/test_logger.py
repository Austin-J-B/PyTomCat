"""Machine-log regression: one line per event, in the right day's file.

tomcat/logger.py keeps the day's ndjson file open instead of reopening it per
event. That is only safe if records still land whole, in the file named for
their own date, when the date rolls over, and when several threads log at once.

Run: python scripts/test_logger.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: List[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}{(': ' + detail) if detail else ''}")


def read_records(root: Path) -> List[dict]:
    out = []
    for path in sorted(root.rglob("*.ndjson")):
        for line in path.read_text(encoding="utf-8").splitlines():
            out.append({"_file": path.name, **json.loads(line)})
    return out


#log-isolation: self-managed
#This one tests the logger, so it cannot use _test_support's TOMCAT_LOG_DIR
#redirect: the assertions below look for the file under the working directory,
#and the variable would send it somewhere else. It chdirs into a temp directory
#before importing tomcat.logger instead, which isolates it just as completely.


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="tomcat-logger-")
    os.chdir(workdir)
    import tomcat.logger as logger

    root = Path("logs/machine")
    print("=" * 70)
    print("machine log regression tests")
    print("=" * 70)

    print("\n[1] every event becomes exactly one parseable line")
    for i in range(50):
        logger.log_action("bench", f"i={i}", "ok")
    logger.log_intent("show_photo", 0.95, cat="Microwave")
    records = read_records(root)
    check("51 records written", len(records) == 51, f"got {len(records)}")
    check("every record carries a ts", all(r.get("ts") for r in records))
    check("intent record round-trips", any(
        r.get("event") == "intent" and r.get("kind") == "show_photo" and r.get("cat") == "Microwave"
        for r in records
    ))

    print("\n[2] a caller's own ts wins, and unicode survives")
    logger.log_event({"ts": "1999-12-31T23:59:59.000-06:00", "event": "replay", "note": "café 猫"})
    replay = [r for r in read_records(root) if r.get("event") == "replay"]
    check("supplied ts preserved", replay and replay[0]["ts"].startswith("1999-12-31"))
    check("unicode preserved", replay and replay[0]["note"] == "café 猫")

    print("\n[3] the file is named for the record's own date")
    today = datetime.now(logger.TZ)
    expected = f"{today:%Y-%m-%d}.ndjson"
    check(f"records live in {expected}",
          all(r["_file"] == expected for r in read_records(root)),
          str({r["_file"] for r in read_records(root)}))

    print("\n[4] the handle rotates when the date rolls over")
    real_datetime = logger.datetime
    tomorrow = today + timedelta(days=1)

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return tomorrow

    logger.datetime = FakeDatetime  # type: ignore[assignment]
    try:
        logger.log_action("tomorrow", "t", "o")
    finally:
        logger.datetime = real_datetime  # type: ignore[assignment]
    tomorrow_file = root / f"{tomorrow:%Y-%m}" / f"{tomorrow:%Y-%m-%d}.ndjson"
    check("next day's file created", tomorrow_file.exists(), str(tomorrow_file))
    logger.log_action("back_to_today", "t", "o")
    today_file = root / f"{today:%Y-%m}" / expected
    #50 bench + 1 intent + 1 replay + 1 back_to_today; the day trip wrote elsewhere.
    check("today's file still appended, not truncated",
          sum(1 for _ in today_file.open(encoding="utf-8")) == 53,
          str(sum(1 for _ in today_file.open(encoding="utf-8"))))

    print("\n[5] concurrent writers do not interleave within a line")
    before = len(read_records(root))
    threads = [
        threading.Thread(target=lambda n=n: [logger.log_action("thread", f"t{n}", "x" * 200)
                                             for _ in range(100)])
        for n in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        after = read_records(root)
    except json.JSONDecodeError as exc:
        check("800 concurrent records parse cleanly", False, f"torn line: {exc}")
    else:
        check("800 concurrent records parse cleanly", len(after) - before == 800,
              f"got {len(after) - before}")
        check("each concurrent record is complete",
              all(len(r.get("output", "")) == 200
                  for r in after if r.get("name") == "thread"))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
