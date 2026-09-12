"""Ledger write regression: what reaches the sheet, and what must not.

_append_ledger_rows is the last gate before a payment is written to the Income
or Expenses sheet. Income and expenses used to have separate copies of it; they
now share one, differing only in the sheet they target, the row builder, and
whether dues are diverted.

The failure that matters here is writing a payment twice, so most of this test
is about skips: an email already in the local index, a repeated transaction or
message id, a row already on the sheet, a duplicate inside the same batch, and
a read that failed and therefore cannot be trusted. Nothing is indexed unless
the append actually succeeded, so an aborted batch retries next scan.

Run: python scripts/test_finance_append.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Sends this process's machine log to a scratch directory, so the test does
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

from tomcat.handlers import finance

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def event(email_id: str, *, counterparty: str = "Jane Doe", note: str = "cat food",
          amount: float = 25.0, direction: str = "income", category: str = "Donations",
          txn: Optional[str] = None, msg: Optional[str] = None,
          subject: Optional[str] = None) -> finance.FinanceEvent:
    return finance.FinanceEvent(
        email_id=email_id, provider="venmo", counterparty=counterparty, note=note,
        amount=amount, direction=direction, category=category,
        ts=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        raw_subject=subject if subject is not None else f"{counterparty} paid you ${amount}",
        raw_content="body", message_blank=not note, message_id=msg, txn_id=txn,
    )


class Sheet:
    """Stand in for one ledger worksheet and the local dedup index."""

    def __init__(self, *, rows_on_sheet: Tuple = (), row_counts: Optional[Dict] = None,
                 fingerprints: Tuple = (), txns: Tuple = (), msgs: Tuple = (),
                 indexed_emails: Tuple = (), open_fails: bool = False,
                 read_fails: bool = False, append_result: Optional[Tuple] = None,
                 sheet_id: Optional[str] = "mega"):
        self.appended: List[Tuple[str, List[List[str]]]] = []
        self.indexed: List[Dict[str, Any]] = []
        self.settled: List[Tuple[str, str]] = []
        self.logs: List[Tuple] = []
        self._config = dict(
            rows_on_sheet=rows_on_sheet, row_counts=row_counts or {},
            fingerprints=fingerprints, txns=txns, msgs=msgs,
            indexed_emails=indexed_emails, open_fails=open_fails,
            read_fails=read_fails, append_result=append_result, sheet_id=sheet_id,
        )

    def install(self) -> None:
        cfg = self._config
        finance.settings.sheet_megasheet_id = cfg["sheet_id"]
        finance.settings.income_ws_title = "Income"
        finance.settings.expense_ws_title = "Expenses"

        def open_ws(_sid, name, _kind):
            if cfg["open_fails"]:
                raise RuntimeError("worksheet unavailable")
            return f"ws:{name}"

        def read_snapshot(_ws, _kind):
            if cfg["read_fails"]:
                return None
            return (list(cfg["rows_on_sheet"]), dict(cfg["row_counts"]))

        async def append_rows(_ws, rows, kind):
            self.appended.append((kind, [list(r) for r in rows]))
            return [cfg["append_result"] or (True, "ok")] * len(rows)

        finance._open_worksheet_with_retry = open_ws
        finance._fetch_recent_sheet_snapshot = read_snapshot
        finance._load_fingerprints = lambda: set(cfg["fingerprints"])
        finance._load_txn_ids = lambda: set(cfg["txns"])
        finance._load_message_ids = lambda: set(cfg["msgs"])
        finance._load_index = lambda: {k: {} for k in cfg["indexed_emails"]}
        finance._append_rows_with_retry = append_rows
        finance._append_index = lambda rec: self.indexed.append(dict(rec))
        finance._index_settled = lambda ev, why: self.settled.append((ev.email_id, why))
        finance.log_action = lambda *args: self.logs.append(tuple(args))

    def run(self, direction: str, events: List[finance.FinanceEvent]) -> Dict:
        self.install()
        fn = (finance._append_income_rows if direction == "income"
              else finance._append_expense_rows)
        return asyncio.run(fn(events))


def main() -> int:
    print("=" * 70)
    print("ledger write regression tests")
    print("=" * 70)

    print("\n[1] a new payment is written, then indexed")
    for direction in ("income", "expense"):
        sheet = Sheet()
        results = sheet.run(direction, [event("a", direction=direction)])
        check(f"{direction}: reported ok", {"a": (True, "ok")}, results)
        check(f"{direction}: one row appended", 1, len(sheet.appended))
        check(f"{direction}: to the right sheet", direction, sheet.appended[0][0])
        check(f"{direction}: indexed once", 1, len(sheet.indexed))
        check(f"{direction}: index carries the email id", "a", sheet.indexed[0]["email_id"])

    print("\n[2] nothing is written when the sheet cannot be reached")
    for label, kwargs, reason in [
        ("no sheet configured", {"sheet_id": None}, "missing_sheet_id"),
        ("worksheet will not open", {"open_fails": True}, "sheet_open_failed"),
        ("existing rows cannot be read", {"read_fails": True}, "sheet_read_failed"),
    ]:
        sheet = Sheet(**kwargs)
        results = sheet.run("income", [event("a")])
        check(label, {"a": (False, reason)}, results)
        check(f"  {label}: nothing appended", [], sheet.appended)
        check(f"  {label}: nothing indexed", [], sheet.indexed)

    print("\n[3] anything already recorded is skipped")
    for label, kwargs, ev in [
        ("email already in the index", {"indexed_emails": ("a",)}, event("a")),
        ("transaction id seen before", {"txns": ("T1",)}, event("a", txn="T1")),
        ("message id seen before", {"msgs": ("M1",)}, event("a", msg="M1")),
    ]:
        sheet = Sheet(**kwargs)
        results = sheet.run("income", [ev])
        check(label, {"a": (False, "dup_skipped")}, results)
        check(f"  {label}: nothing appended", [], sheet.appended)

    print("\n[4] a duplicate inside one batch is caught")
    sheet = Sheet()
    results = sheet.run("income", [event("a", txn="T9"), event("b", txn="T9")])
    check("second copy skipped", {"a": (True, "ok"), "b": (False, "dup_skipped")}, results)
    check("only one row appended", 1, len(sheet.appended[0][1]))

    sheet = Sheet()
    results = sheet.run("income", [event("a"), event("b")])
    check("an identical row is skipped too",
          {"a": (True, "ok"), "b": (False, "dup_skipped")}, results)
    check("and settled so later scans skip it", [("b", "duplicate")], sheet.settled)

    print("\n[5] dues go to the dues handler, not the income sheet")
    dues = event("a", note="dues", amount=15.0,
                 subject="Jane Doe paid you $15.00 for dues")
    sheet = Sheet()
    check("income diverts dues", {"a": (False, "dues_skip")}, sheet.run("income", [dues]))
    check("nothing written", [], sheet.appended)
    #The expense side has no dues concept, so the same event is written.
    sheet = Sheet()
    check("expenses do not divert", {"a": (True, "ok")}, sheet.run("expense", [dues]))

    print("\n[6] a failed append is reported and not indexed")
    sheet = Sheet(append_result=(False, "quota exceeded"))
    results = sheet.run("income", [event("a")])
    check("failure reported verbatim", {"a": (False, "quota exceeded")}, results)
    check("row was attempted", 1, len(sheet.appended))
    check("but not indexed, so it retries", [], sheet.indexed)
    check("and the error is logged", True,
          any(a[0] == "finance_sheet_error" and a[1] == "income_append_error"
              for a in sheet.logs))

    print("\n[7] skips are logged with the side of the books they came from")
    sheet = Sheet(txns=("T1",))
    sheet.run("expense", [event("a", txn="T1", direction="expense")])
    check("expense txn skip tagged", True,
          any(a[0] == "finance_skip_duplicate" and a[1] == "expense_txn"
              for a in sheet.logs))

    print("\n[8] an empty batch does no work")
    sheet = Sheet()
    check("no results", {}, sheet.run("income", []))
    check("nothing appended", [], sheet.appended)

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
