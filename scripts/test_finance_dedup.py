"""Regression test for finance duplicate detection.

Replays the real duplicate rows found in the CCC megasheet exports against
`_looks_duplicate` and checks that the fixes catch what the old logic missed.

Run:  python scripts/test_finance_dedup.py [--csv-dir DIR]
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.handlers.finance import (  # noqa: E402
    FinanceEvent,
    _bot_marker,
    _build_expense_row,
    _build_income_row,
    _clean_sheet_text,
    _extract_note_hints_from_field,
    _looks_duplicate,
    _parse_sheet_records,
    _sheet_email_id,
)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name,
                           "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------- unit tests

def test_marker_roundtrip() -> None:
    print("\n[1] bot marker survives a build -> parse round trip")
    ev = FinanceEvent(
        email_id="18f3a2b1c9d", provider="paypal", counterparty="Jacob Sandoval",
        note="Bake sale", amount=3.0, direction="income", category="Donations",
        ts=datetime(2026, 4, 16, tzinfo=timezone.utc), raw_subject="", raw_content="",
        message_blank=False,
    )
    row = _build_income_row(ev)
    recs = _parse_sheet_records([row], "income")
    r = recs[0]
    check("email id recovered from sheet row", r["sheet_email_id"] == "18f3a2b1c9d",
          repr(r["sheet_email_id"]))
    check("row still flagged as bot-written", r["recorded_by_bot"] is True)
    check("counterparty unpolluted by tag", r["counterparty"] == "Jacob Sandoval",
          repr(r["counterparty"]))
    check("note preserved", r["note"] == "Bake sale", repr(r["note"]))
    check("row_type captured (income)", r["row_type"] == "Donations", repr(r["row_type"]))

    ev2 = FinanceEvent(
        email_id="abc123xyz", provider="paypal", counterparty="Py Store Here Pantego",
        note="", amount=54.0, direction="expense", category="Storage Unit Fee",
        ts=datetime(2026, 3, 20, tzinfo=timezone.utc), raw_subject="", raw_content="",
        message_blank=True,
    )
    erow = _build_expense_row(ev2)
    er = _parse_sheet_records([erow], "expense")[0]
    check("expense row_type captured", er["row_type"] == "Storage Unit Fee",
          repr(er["row_type"]))
    check("expense email id recovered", er["sheet_email_id"] == "abc123xyz")
    check("marker stripped by _clean_sheet_text",
          "tc:" not in _clean_sheet_text(erow[3]), _clean_sheet_text(erow[3]))

    # Legacy rows (written before the tag shipped) must still parse.
    legacy = ["3/20/2026", "March", "2026", "Wix (Message: none) [Recorded by the TomCat bot]",
              "Website Fee", "$25.98"]
    lr = _parse_sheet_records([legacy], "expense")[0]
    check("legacy row still detected as bot-written", lr["recorded_by_bot"] is True)
    check("legacy row yields no id", lr["sheet_email_id"] == "")
    check("blank email id degrades to plain marker",
          _sheet_email_id(_bot_marker("")) == "")


def test_note_asymmetry() -> None:
    print("\n[2] a note on only one side no longer vetoes a match")
    ev = FinanceEvent(
        email_id="e1", provider="venmo", counterparty="Megan Digiovanni", note="",
        amount=74.92, direction="expense", category="Food",
        ts=datetime(2025, 7, 30, tzinfo=timezone.utc), raw_subject="", raw_content="",
        message_blank=True,
    )
    officer_row = {
        "date": datetime(2025, 7, 30).date(), "amount": 74.92, "provider": "",
        "counterparty": "Megan Digiovanni", "note": "cat food reimbursement",
        "row_type": "Food", "recorded_by_bot": False, "sheet_email_id": "",
    }
    check("bot row matches officer row that carries a note",
          _looks_duplicate(ev, [officer_row]) is True)

    # Guard against over-matching: genuinely different notes must still split.
    other = dict(officer_row, counterparty="Derek Fuentes", note="vet bill")
    check("different counterparty still not a duplicate",
          _looks_duplicate(ev, [other]) is False)

    # Notes that normalize to nothing (emoji only) carry no comparable text and
    # must not veto an otherwise-identical row.
    emoji_ev = FinanceEvent(
        email_id="e9", provider="venmo", counterparty="Carson Bevoni", note="\U0001F638",
        amount=3.0, direction="income", category="Foods/Goods fundraisers",
        ts=datetime(2026, 3, 23, tzinfo=timezone.utc), raw_subject="", raw_content="",
        message_blank=False,
    )
    emoji_row = {
        "date": datetime(2026, 3, 23).date(), "amount": 3.0, "provider": "",
        "counterparty": "Carson Bevoni", "note": "\U0001F638", "row_type": "Donations",
        "recorded_by_bot": True, "sheet_email_id": "",
    }
    check("emoji-only notes do not block a match",
          _looks_duplicate(emoji_ev, [emoji_row]) is True)

    # But two genuinely different bake-sale items on one day must stay separate.
    pretzel = FinanceEvent(
        email_id="e10", provider="venmo", counterparty="Cassandra Nelson", note="petzel",
        amount=6.0, direction="income", category="Donations",
        ts=datetime(2025, 11, 4, tzinfo=timezone.utc), raw_subject="", raw_content="",
        message_blank=False,
    )
    bagel_row = {
        "date": datetime(2025, 11, 4).date(), "amount": 6.0, "provider": "",
        "counterparty": "Cassandra Nelson", "note": "bagel", "row_type": "Donations",
        "recorded_by_bot": True, "sheet_email_id": "",
    }
    check("same buyer, same price, different item stays distinct",
          _looks_duplicate(pretzel, [bagel_row]) is False)


def test_single_vendor() -> None:
    print("\n[3] single-vendor recurring charges match on category")
    ev = FinanceEvent(
        email_id="e2", provider="cashapp", counterparty="Py Store Here Pantego",
        note="", amount=54.0, direction="expense", category="Storage Unit Fee",
        ts=datetime(2025, 4, 20, tzinfo=timezone.utc), raw_subject="", raw_content="",
        message_blank=True,
    )
    officer_row = {
        "date": datetime(2025, 4, 20).date(), "amount": 54.0, "provider": "",
        "counterparty": "storage unit fee", "note": "", "row_type": "Storage Unit Fee",
        "recorded_by_bot": False, "sheet_email_id": "",
    }
    check("merchant name vs service name now matches",
          _looks_duplicate(ev, [officer_row]) is True)

    # A different day is a different month's bill - must NOT collapse.
    next_month = dict(officer_row, date=datetime(2025, 5, 20).date())
    check("next month's storage bill is not a duplicate",
          _looks_duplicate(ev, [next_month]) is False)

    # Vendor bills logged a couple of days after the statement date.
    late_logged = dict(officer_row, date=datetime(2025, 4, 22).date())
    check("vendor bill logged 2 days later still matches",
          _looks_duplicate(ev, [late_logged]) is True)

    # Non-vendor categories must not get the bypass.
    food_ev = FinanceEvent(
        email_id="e3", provider="cashapp", counterparty="Walmart", note="",
        amount=54.0, direction="expense", category="Food",
        ts=datetime(2025, 4, 20, tzinfo=timezone.utc), raw_subject="", raw_content="",
        message_blank=True,
    )
    food_row = dict(officer_row, counterparty="Kroger run", row_type="Food")
    check("unrelated Food rows of equal value stay distinct",
          _looks_duplicate(food_ev, [food_row]) is False)


# ------------------------------------------------------- real-data replay

def money(s):
    try:
        return round(float(re.sub(r"[^\d.\-]", "", s or "")), 2)
    except Exception:
        return None


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime((s or "").strip(), fmt)
        except Exception:
            pass
    return None


def replay(csv_dir: str, fname: str, kind: str, name_col: str, amt_col: str, type_col: str) -> None:
    path = os.path.join(csv_dir, fname)
    if not os.path.exists(path):
        print("\n[skip] %s not found" % path)
        return
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        raw = list(csv.reader(f))
    header = raw[2]
    rows = [dict(zip(header, r)) for r in raw[3:] if any((c or "").strip() for c in r)]

    recs, events = [], []
    for r in rows:
        d, a = parse_date(r.get("Timestamp")), money(r.get(amt_col))
        if d is None or a is None:
            continue
        name_field = (r.get(name_col) or "").strip()
        cp, note = _extract_note_hints_from_field(name_field)
        cat = (r.get(type_col) or "").strip()
        is_bot = "[Recorded by the TomCat bot" in name_field
        rec = {"date": d.date(), "amount": a, "provider": "", "counterparty": cp,
               "note": note, "row_type": cat, "recorded_by_bot": is_bot,
               "sheet_email_id": ""}
        recs.append(rec)
        if is_bot:
            events.append(FinanceEvent(
                email_id="x", provider="", counterparty=cp, note=note, amount=a,
                direction=kind, category=cat,
                ts=d.replace(tzinfo=timezone.utc), raw_subject="", raw_content="",
                message_blank=not note))

    # For each bot row, ask: does any OTHER row look like the same payment?
    caught = 0
    for ev, own in zip(events, [r for r in recs if r["recorded_by_bot"]]):
        others = [r for r in recs if r is not own]
        if _looks_duplicate(ev, others):
            caught += 1
    print("\n[4] %s replay: %d bot rows, %d now recognised as duplicates of an existing row"
          % (kind, len(events), caught))
    return caught


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default=os.path.expanduser("~/Downloads"))
    args = ap.parse_args()

    print("=" * 70)
    print("finance dedup regression tests")
    print("=" * 70)
    test_marker_roundtrip()
    test_note_asymmetry()
    test_single_vendor()
    replay(args.csv_dir, "CCC megasheet - Expenses.csv", "expense",
           "Name (Either who money was transferred to, or which officer is reporting the expense)",
           "Amount ", "Expense Type")
    replay(args.csv_dir, "CCC megasheet - Income.csv", "income",
           "Name (Either who donated, or which officer is reporting this income)",
           "Amount", "Income type")

    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All unit checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
