"""Delete operational logs past the retention window stated in docs/PRIVACY.md.

Design notes:

* Dates come from filenames, never from mtime. Every file on the production box
  carries an mtime of the migration date, so mtime would delete everything or
  nothing.
* Matching is a strict allowlist. A file is only ever removed if it matches one
  of the RULES below; anything unrecognised is left alone. Dedup indexes live in
  the same tree (logs/emails/index.jsonl, logs/finance/index.jsonl,
  logs/dues/index.jsonl) and deleting one silently re-enables duplicate
  processing, so a denylist would be the wrong way round.
* PROTECTED is a second, independent guard on top of the allowlist.
"""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Callable, List, Optional, Tuple

from ..logger import log_action

#Retention window promised in the published privacy policy. Changing this
#number means changing that document too.
RETENTION_MONTHS = 24

#State files that must survive regardless of age. None of the RULES below can
#match these, but they are listed explicitly so an added rule cannot start
#eating dedup state without this assertion failing first.
PROTECTED = {
    "index.jsonl",
    "pending_dues.jsonl",
    "resolved_dues_emails.jsonl",
    "invite_flags.json",
}

_MONTH_ALIASES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_from_name(token: str) -> Optional[int]:
    """Map 'Aug' or 'Sept' to a month number. Older logs used 4-letter Sept."""
    return _MONTH_ALIASES.get(token.strip().lower())


def _ymd(m: re.Match) -> Optional[date]:
    try:
        return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
    except (ValueError, IndexError):
        return None


def _ym(m: re.Match) -> Optional[date]:
    try:
        return date(int(m.group("y")), int(m.group("m")), 1)
    except (ValueError, IndexError):
        return None


def _y_monthname(m: re.Match) -> Optional[date]:
    mon = _month_from_name(m.group("mon"))
    if not mon:
        return None
    try:
        return date(int(m.group("y")), mon, 1)
    except ValueError:
        return None


#(regex against the path relative to logs/, extractor). Forward slashes only;
#paths are normalised before matching so this works on Windows too.
RULES: List[Tuple[re.Pattern, Callable[[re.Match], Optional[date]]]] = [
    #machine/2026-08/2026-08-06.ndjson
    (re.compile(r"^machine/\d{4}-\d{2}/(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\.ndjson$"), _ymd),
    #emails/2026-Aug.ndjson and dues/2026-Aug.ndjson
    (re.compile(r"^(?:emails|dues)/(?P<y>\d{4})-(?P<mon>[A-Za-z]{3,4})\.ndjson$"), _y_monthname),
    #dues/portal_export-20250908-003712.ndjson
    (re.compile(r"^dues/portal_export-(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})-\d+\.ndjson$"), _ymd),
    #subs/2026/2026-08.jsonl
    (re.compile(r"^subs/\d{4}/(?P<y>\d{4})-(?P<m>\d{2})\.jsonl$"), _ym),
    #moderation/timeouts/2026/2026-05.jsonl
    (re.compile(r"^moderation/timeouts/\d{4}/(?P<y>\d{4})-(?P<m>\d{2})\.jsonl$"), _ym),
    #gallery_retrain/locks/2026-03-18.lock and dues/locks/2026-06-01.lock
    (re.compile(r"^(?:gallery_retrain|dues)/locks/(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\.lock$"), _ymd),
    #gallery_retrain/manual_20260209_041518.log
    (re.compile(r"^gallery_retrain/manual_(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})_\d+\.log$"), _ymd),
]


def _cutoff(months: int, today: Optional[date] = None) -> date:
    """First day of the month `months` back from today."""
    t = today or date.today()
    y, m = t.year, t.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def find_expired(root: str = "logs", months: int = RETENTION_MONTHS,
                 today: Optional[date] = None) -> List[Tuple[str, date]]:
    """Return [(path, file_date)] for log files older than the retention window."""
    cutoff = _cutoff(months, today)
    out: List[Tuple[str, date]] = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if name in PROTECTED:
                continue
            for pattern, extract in RULES:
                m = pattern.match(rel)
                if not m:
                    continue
                when = extract(m)
                if when and when < cutoff:
                    #Belt and braces: never return a protected name even if a
                    #future rule is written loosely enough to match one.
                    if os.path.basename(full) not in PROTECTED:
                        out.append((full, when))
                break
    return sorted(out, key=lambda x: x[1])


def prune_old_logs(root: str = "logs", months: int = RETENTION_MONTHS,
                   dry_run: bool = False) -> Tuple[int, int]:
    """Delete expired logs. Returns (files_removed, bytes_freed)."""
    expired = find_expired(root, months)
    removed = 0
    freed = 0
    for path, _when in expired:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if dry_run:
            removed += 1
            freed += size
            continue
        try:
            os.remove(path)
            removed += 1
            freed += size
        except OSError as e:
            log_action("log_retention_error", path, str(e))
    if removed and not dry_run:
        log_action("log_retention_pruned", f"files={removed}",
                   f"freed_bytes={freed} window_months={months}")
    return removed, freed


async def start_log_retention_scheduler() -> None:
    """Prune at startup, then once a day."""
    import asyncio
    while True:
        try:
            await asyncio.to_thread(prune_old_logs)
        except Exception as e:
            log_action("log_retention_error", "scheduler", str(e))
        await asyncio.sleep(24 * 60 * 60)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Prune logs past the retention window.")
    ap.add_argument("--root", default="logs")
    ap.add_argument("--months", type=int, default=RETENTION_MONTHS)
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a preview)")
    args = ap.parse_args()

    hits = find_expired(args.root, args.months)
    print("retention window: %d months (cutoff %s)" % (args.months, _cutoff(args.months)))
    print("expired files: %d" % len(hits))
    for p, w in hits:
        print("  %s  %s" % (w.isoformat(), p))
    if args.apply:
        n, b = prune_old_logs(args.root, args.months)
        print("\ndeleted %d files, freed %.1f KB" % (n, b / 1024.0))
    else:
        print("\npreview only -- pass --apply to delete")
