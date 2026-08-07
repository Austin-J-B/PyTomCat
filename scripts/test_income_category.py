"""Regression test for income category classification.

Replays the reviewed megasheet Income history through the real
_assign_income_category and reports how often it agrees with the human label.

Note on ground truth: a large share of historical "Donations" rows are the
bot's own old default that nobody corrected, so the headline accuracy is a
floor. The officer-labelled subset is the trustworthy signal.

Run:  python scripts/test_income_category.py [--csv PATH]
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.handlers.finance import (  # noqa: E402
    FinanceEvent,
    _assign_income_category,
    _categorize_income,
    _DONATION_DEFAULT,
    _INCOME_TYPES,
    _score_fundraiser_signal,
)

FUND = {_INCOME_TYPES["foods_goods"], _INCOME_TYPES["other"]}
BOT = "[Recorded by the TomCat bot"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


def mk(amount, note, pay="Venmo", counterparty="Someone", when=None):
    ev = FinanceEvent(
        email_id="t", provider=pay.lower(), counterparty=counterparty, note=note,
        amount=amount, direction="income", category=None,
        ts=when or datetime(2026, 3, 1, tzinfo=timezone.utc),
        raw_subject="", raw_content="", message_blank=not note,
    )
    ev.category = _categorize_income(note or "", "") or _DONATION_DEFAULT
    return ev


def classify(ev, batch=None, sheet=None):
    cat, _ = _assign_income_category(ev, batch or [], sheet or [])
    return cat


# ------------------------------------------------------------------ units

def test_keywords_still_win():
    print("\n[1] keyword classifier still takes priority")
    ev = mk(3.00, "cookie + sticker")
    check("food keyword -> fundraiser", classify(ev) in FUND, ev.category)
    check("marked as keyword confidence", ev.category_confidence == "keyword",
          ev.category_confidence)

    # An explicit donation word must beat a bake-sale-shaped day.
    busy = [mk(2.0, "", "Venmo") for _ in range(12)]
    ev2 = mk(3.00, "donation for the cats")
    check("donation wording beats busy-day context",
          classify(ev2, busy) == _DONATION_DEFAULT, ev2.category)
    check("and is flagged as keyword-driven", ev2.category_confidence == "keyword")


def test_amount_shape():
    print("\n[2] amount shape")
    ev = mk(3.88, "", "Paypal")           # $4 net of a 3% fee
    check("$3.88 via PayPal -> donation", classify(ev) == _DONATION_DEFAULT, ev.category)
    ev = mk(24.25, "", "Paypal")
    check("$24.25 via PayPal -> donation", classify(ev) == _DONATION_DEFAULT, ev.category)
    ev = mk(50.00, "", "Paypal")
    check("$50 round via PayPal -> donation", classify(ev) == _DONATION_DEFAULT, ev.category)
    ev = mk(15.00, "", "Venmo")
    check("$15 (membership amount) -> donation", classify(ev) == _DONATION_DEFAULT, ev.category)


def test_day_shape():
    print("\n[3] day shape needs the right rails, not just volume")
    day = datetime(2026, 4, 10, tzinfo=timezone.utc)
    sale = [{"date": day.date(), "amount": 2.0, "provider": "Venmo",
             "counterparty": "x%d" % i, "note": "", "income_type": "",
             "row_type": ""} for i in range(14)]
    ev = mk(3.00, "", "Venmo", "Buyer", day)
    check("busy in-person day -> fundraiser", classify(ev, [], sale) in FUND, ev.category)

    drive = [{"date": day.date(), "amount": 9.7, "provider": "Paypal",
              "counterparty": "y%d" % i, "note": "", "income_type": "",
              "row_type": ""} for i in range(60)]
    ev2 = mk(6.79, "", "Paypal", "Giver", day)
    check("busy ONLINE day -> donation (the 2025-10-22 case)",
          classify(ev2, [], drive) == _DONATION_DEFAULT, ev2.category)

    ev3 = mk(20.00, "", "Venmo", "Solo", day)
    check("lone payment on a quiet day -> donation",
          classify(ev3, [], []) == _DONATION_DEFAULT, ev3.category)


def test_abstention():
    print("\n[4] low-confidence rows are flagged, not silently defaulted")
    ev = mk(7.00, "", "Cashapp", "Ambiguous")
    classify(ev, [], [])
    check("confidence recorded", ev.category_confidence in ("high", "low"),
          ev.category_confidence)
    check("a reason is always given", bool(ev.category_reason), ev.category_reason)
    score, _ = _score_fundraiser_signal(ev, [], [])
    check("borderline score -> low confidence",
          (abs(score) >= 2.0) == (ev.category_confidence == "high"),
          "score=%.1f conf=%s" % (score, ev.category_confidence))


# ------------------------------------------------------------ real replay

def replay(path):
    raw = list(csv.reader(open(path, encoding="utf-8-sig", errors="replace", newline="")))
    header = raw[2]
    rows = []
    for r in raw[3:]:
        if not any((c or "").strip() for c in r):
            continue
        d = dict(zip(header, r))
        dt = None
        for f in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                dt = datetime.strptime((d.get("Timestamp") or "").strip(), f)
                break
            except Exception:
                pass
        if dt is None:
            continue
        try:
            amt = round(float(re.sub(r"[^\d.\-]", "", d.get("Amount") or "")), 2)
        except Exception:
            continue
        cat = (d.get("Income type") or "").strip()
        if cat not in FUND and cat != _DONATION_DEFAULT:
            continue
        name = (d.get("Name (Either who donated, or which officer is reporting this income)") or "").strip()
        note = ""
        m = re.search(r"\(Message:\s*(.*?)\)", name)
        if m and m.group(1).strip().lower() != "none":
            note = m.group(1).strip()
        elif BOT not in name:
            inner = re.findall(r"\(([^)]*)\)", name)
            note = inner[0] if inner else ""
        rows.append({
            "date": dt.date(), "amount": amt, "note": note,
            "counterparty": re.split(r"[(\[]", name)[0].strip(),
            "pay": (d.get("Payment Type") or "").strip(),
            "label": "fundraiser" if cat in FUND else "donation",
            "by_bot": BOT in name,
        })

    # The snapshot must carry real categories, otherwise the nearby-entries path
    # never fires and the replay only exercises the scorer.
    sheet = [{"date": r["date"], "amount": r["amount"], "provider": r["pay"],
              "counterparty": r["counterparty"], "note": r["note"],
              "income_type": (_INCOME_TYPES["foods_goods"] if r["label"] == "fundraiser"
                              else _DONATION_DEFAULT),
              "row_type": ""} for r in rows]

    def run(subset, title):
        tp = fp = tn = fn = 0
        for r in subset:
            ev = mk(r["amount"], r["note"], r["pay"] or "Venmo", r["counterparty"],
                    datetime.combine(r["date"], datetime.min.time()).replace(tzinfo=timezone.utc))
            others = [s for s in sheet if not (s["date"] == r["date"]
                      and s["amount"] == r["amount"]
                      and s["counterparty"] == r["counterparty"])]
            got = "fundraiser" if classify(ev, [], others) in FUND else "donation"
            if got == "fundraiser" and r["label"] == "fundraiser":
                tp += 1
            elif got == "fundraiser":
                fp += 1
            elif r["label"] == "donation":
                tn += 1
            else:
                fn += 1
        n = len(subset)
        if not n:
            return
        print("\n  %s (n=%d)" % (title, n))
        print("     accuracy %.1f%%   precision %.1f%%   recall %.1f%%"
              % (100 * (tp + tn) / n, 100 * tp / max(1, tp + fp), 100 * tp / max(1, tp + fn)))
        print("     confusion: tp=%d fp=%d tn=%d fn=%d" % (tp, fp, tn, fn))

    print("\n[5] replay of %d reviewed rows" % len(rows))
    run(rows, "ALL rows (ground truth polluted by the old default)")
    run([r for r in rows if not r["by_bot"]], "OFFICER-labelled rows only (trustworthy)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()
    print("=" * 70)
    print("income category regression tests")
    print("=" * 70)
    test_keywords_still_win()
    test_amount_shape()
    test_day_shape()
    test_abstention()

    path = args.csv
    if not path:
        hits = sorted(glob.glob(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "backups", "income-reviewed-*.csv")))
        path = hits[-1] if hits else None
    if path and os.path.exists(path):
        replay(path)
    else:
        print("\n[5] skipped replay (no backups/income-reviewed-*.csv)")

    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All unit checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
