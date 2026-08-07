"""Regression test for log retention pruning.

The important property is not "old files get deleted" -- it is that the dedup
indexes never do. Losing logs/finance/index.jsonl is what let a bulk email
re-log append ~50 duplicate rows to the megasheet.

Run:  python scripts/test_log_retention.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.services.log_retention import (  # noqa: E402
    PROTECTED, RETENTION_MONTHS, _cutoff, find_expired, prune_old_logs,
)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


def touch(root: str, rel: str, content: str = "x\n") -> str:
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def build(root: str) -> None:
    """Mirror the real production tree, old and new."""
    # Old enough to expire (dated 2023, well past a 24-month window from 2026).
    touch(root, "machine/2023-01/2023-01-15.ndjson")
    touch(root, "emails/2023-Apr.ndjson")
    touch(root, "emails/2023-Sept.ndjson")          # legacy 4-letter month
    touch(root, "dues/2023-Feb.ndjson")
    touch(root, "dues/portal_export-20230908-003712.ndjson")
    touch(root, "subs/2023/2023-11.jsonl")
    touch(root, "moderation/timeouts/2023/2023-05.jsonl")
    touch(root, "gallery_retrain/locks/2023-03-18.lock")
    touch(root, "gallery_retrain/manual_20230209_041518.log")
    touch(root, "dues/locks/2023-06-01.lock")

    # Recent -- must survive.
    touch(root, "machine/2026-08/2026-08-06.ndjson")
    touch(root, "emails/2026-Aug.ndjson")
    touch(root, "subs/2026/2026-08.jsonl")

    # Dedup / state -- must survive regardless of age.
    touch(root, "emails/index.jsonl")
    touch(root, "dues/index.jsonl")
    touch(root, "finance/index.jsonl")
    touch(root, "finance/pending_dues.jsonl")
    touch(root, "finance/resolved_dues_emails.jsonl")
    touch(root, "invite_flags.json")

    # Unrecognised shapes -- must be left alone, not guessed at.
    touch(root, "emails/notes-from-2023.txt")
    touch(root, "machine/2023-01/README.md")
    touch(root, "something_unexpected.ndjson")


def main() -> int:
    print("=" * 70)
    print("log retention tests")
    print("=" * 70)
    root = tempfile.mkdtemp(prefix="tomcat-retention-")
    try:
        build(root)
        before = set()
        for dp, _dn, fn in os.walk(root):
            for n in fn:
                before.add(os.path.relpath(os.path.join(dp, n), root).replace(os.sep, "/"))

        today = date(2026, 8, 7)
        expired = {os.path.relpath(p, root).replace(os.sep, "/")
                   for p, _ in find_expired(root, RETENTION_MONTHS, today=today)}

        print("\n[1] cutoff maths")
        check("24 months back from 2026-08-07 is 2024-08-01",
              _cutoff(24, today) == date(2024, 8, 1), str(_cutoff(24, today)))

        print("\n[2] expired files are identified")
        for rel in ("machine/2023-01/2023-01-15.ndjson", "emails/2023-Apr.ndjson",
                    "dues/2023-Feb.ndjson", "subs/2023/2023-11.jsonl",
                    "moderation/timeouts/2023/2023-05.jsonl",
                    "gallery_retrain/locks/2023-03-18.lock",
                    "gallery_retrain/manual_20230209_041518.log",
                    "dues/locks/2023-06-01.lock",
                    "dues/portal_export-20230908-003712.ndjson"):
            check("expires %s" % rel, rel in expired)
        check("legacy 4-letter 'Sept' parsed", "emails/2023-Sept.ndjson" in expired)

        print("\n[3] recent files survive")
        for rel in ("machine/2026-08/2026-08-06.ndjson", "emails/2026-Aug.ndjson",
                    "subs/2026/2026-08.jsonl"):
            check("keeps %s" % rel, rel not in expired)

        print("\n[4] dedup state is never touched (the one that matters)")
        for rel in ("emails/index.jsonl", "dues/index.jsonl", "finance/index.jsonl",
                    "finance/pending_dues.jsonl", "finance/resolved_dues_emails.jsonl",
                    "invite_flags.json"):
            check("protects %s" % rel, rel not in expired)
        check("no expired file has a PROTECTED basename",
              not any(os.path.basename(r) in PROTECTED for r in expired))

        print("\n[5] unrecognised files are left alone, not guessed at")
        for rel in ("emails/notes-from-2023.txt", "machine/2023-01/README.md",
                    "something_unexpected.ndjson"):
            check("ignores %s" % rel, rel not in expired)

        print("\n[6] dry run deletes nothing")
        n, _ = prune_old_logs(root, RETENTION_MONTHS, dry_run=True)
        after_dry = set()
        for dp, _dn, fn in os.walk(root):
            for n2 in fn:
                after_dry.add(os.path.relpath(os.path.join(dp, n2), root).replace(os.sep, "/"))
        check("dry run reported %d files" % n, n > 0)
        check("dry run left every file in place", after_dry == before)

        print("\n[7] real prune removes exactly the expired set")
        removed, _freed = prune_old_logs(root, RETENTION_MONTHS)
        after = set()
        for dp, _dn, fn in os.walk(root):
            for n2 in fn:
                after.add(os.path.relpath(os.path.join(dp, n2), root).replace(os.sep, "/"))
        # find_expired uses date.today(); with 2023 fixtures the result is the
        # same set regardless, which is why the fixtures are that old.
        check("deleted count matches expired count", removed == len(expired),
              "%d vs %d" % (removed, len(expired)))
        check("survivors are exactly before-minus-expired", after == (before - expired),
              str(sorted((after ^ (before - expired)))[:4]))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
