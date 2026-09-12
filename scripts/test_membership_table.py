"""Membership sheet parsing regression: a messy export in, dues rows out.

The Membership Application List is a form-backed sheet, so the header is not
always the first row and its column names have changed. dues._parse_membership_table
finds the header by scoring candidate rows against the names it knows, then reads
columns by name. Both the live sheet and the CSV fallback go through it.

Run: python scripts/test_membership_table.py
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat.handlers import dues

#Header detection logs which row it picked; not interesting here.
dues.log_action = lambda *args: None

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


CANONICAL = [
    ["Date", "Full Name", "Discord Username", "Payment Username", "Paid Where",
     "Dues or Donation", "Email", "Semester", "Verified", "Mavorgs Invite", "Donation"],
    ["2026-01-02", "Jane Doe", "jane_d", "jane-venmo", "Venmo", "Dues",
     "j@x.com", "Spring 2026", "TRUE", "yes", "0"],
    ["2026-01-03", "Bob Smith", "bob.s", "", "Cash App", "Donation",
     "b@x.com", "Spring 2026", "no", "", "25"],
]


def main() -> int:
    print("=" * 70)
    print("membership sheet parsing regression tests")
    print("=" * 70)

    print("\n[1] the canonical export reads straight through")
    rows = dues._parse_membership_table(CANONICAL)
    check("two rows", 2, len(rows))
    check("first row", {
        "date": "2026-01-02", "full_name": "Jane Doe", "discord_username": "jane_d",
        "payment_username": "jane-venmo", "paid_where": "Venmo", "kind": "Dues",
        "email": "j@x.com", "semester": "Spring 2026", "verified": True,
        "mavorgs_invite": True, "donation_amount": "0",
    }, rows[0])
    check("a 'no' is not verified", False, rows[1]["verified"])
    check("a blank invite is not invited", False, rows[1]["mavorgs_invite"])

    print("\n[2] the header is found by name, not position")
    rows = dues._parse_membership_table([
        ["Club dues tracker"],
        ["exported 2026"],
        [],
        ["Full Legal Name", "Discord Handle", "Email", "Semester", "Verified"],
        ["Carol Jones", "carol_j", "c@x.com", "Fall 2025", "done"],
    ])
    check("preamble skipped", 1, len(rows))
    check("name read from 'Full Legal Name'", "Carol Jones", rows[0]["full_name"])
    check("handle read from 'Discord Handle'", "carol_j", rows[0]["discord_username"])
    check("'done' counts as verified", True, rows[0]["verified"])

    print("\n[3] older column spellings still resolve")
    rows = dues._parse_membership_table([
        ["Name", "Discord Tag", "Pay Handle", "Provider", "Type", "Email",
         "Semester", "Is Verified", "Donations"],
        ["Dana Wells", "dana#1", "dana-cash", "Cash App", "Dues", "d@x.com",
         "Spring 2026", "ok", "5"],
    ])
    check("one row", 1, len(rows))
    check("fields mapped", ("Dana Wells", "dana#1", "dana-cash", "Cash App", "Dues", "5"),
          (rows[0]["full_name"], rows[0]["discord_username"], rows[0]["payment_username"],
           rows[0]["paid_where"], rows[0]["kind"], rows[0]["donation_amount"]))

    print("\n[4] ticks, padding, short rows and blank rows")
    rows = dues._parse_membership_table([
        ["Full Name", "Discord Username", "Email", "Semester", "Verified", "Mavorgs Invite"],
        ["  Padded  ", " pad_d ", "p@x.com", "", "✅", "x"],
        ["", "", "", "", "", ""],
        ["Short Row"],
    ])
    check("blank rows dropped, short rows kept", 2, len(rows))
    check("cells are stripped", ("Padded", "pad_d"),
          (rows[0]["full_name"], rows[0]["discord_username"]))
    #A green tick is how officers mark the sheet by hand. The shared parser used
    #to carry a mis-encoded copy of it, so the CSV path read ticked rows as
    #unverified while the live sheet read them as verified.
    check("a green tick counts as verified", True, rows[0]["verified"])
    check("missing trailing cells read empty", "", rows[1]["semester"])

    print("\n[5] nothing usable")
    check("no rows", [], dues._parse_membership_table([]))
    check("header only", [], dues._parse_membership_table(
        [["Full Name", "Discord Username", "Email", "Semester", "Verified"]]))
    #No column name is recognized, so every field comes back empty and the row
    #is dropped for having nothing in it.
    unmatched = dues._parse_membership_table([["a", "b", "c"], ["1", "2", "3"]])
    check("an unrecognizable sheet yields no usable rows", [],
          [r for r in unmatched if any(v for v in r.values())])

    print("\n[6] the CSV fallback uses the same parser")
    tmp = Path(tempfile.mkdtemp(prefix="tomcat-membership-"))
    path = tmp / "membership.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(CANONICAL)
    check("CSV matches the sheet parser",
          dues._parse_membership_table(CANONICAL),
          dues._load_membership_rows_from_csv(path))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
