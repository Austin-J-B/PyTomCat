"""Payment-email classification regression: notification in, ledger event out.

handlers/finance.py turns Venmo, Cash App and PayPal notification emails into
FinanceEvents. The three classifiers share one event builder and one
income/expense pair (_event, _income, _expense) rather than repeating the
thirteen-field construction at every match, so this test pins what each provider
pattern extracts — counterparty, note, amount, direction, category — and which
emails must be left alone.

Dues arrive as ("dues", no event): handlers/dues.py owns those, and booking them
here as well would double-count them.

Run: python scripts/test_finance_classifiers.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Sends this process's machine log to a scratch directory, so the test does
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

from tomcat.handlers import finance

FAILURES: List[str] = []

#(classifier, subject, body, expected status, expected event fields or None)
CASES = [
    # ---- Venmo: everything is in the subject line ----
    ("venmo", "Jane Doe paid you $25.00 for cat food", "Jane Doe paid you $25.00\nNote: cat food",
     "income", dict(counterparty="Jane Doe", note="cat food", amount=25.0,
                    category="Foods/Goods fundraisers", message_blank=False)),
    ("venmo", "Jane Doe paid you $25.00", "Jane Doe paid you $25.00",
     "income", dict(counterparty="Jane Doe", note="", amount=25.0,
                    category="Donations", message_blank=True)),
    ("venmo", "Bob Smith sent you $5", "Bob Smith sent you $5",
     "income", dict(counterparty="Bob Smith", note="", amount=5.0, category="Donations")),
    ("venmo", "Bob Smith sent you $1,250.75 for donation", "note here",
     "income", dict(counterparty="Bob Smith", note="donation", amount=1250.75)),
    ("venmo", "Jane paid you $0.01 for x", "x",
     "income", dict(counterparty="Jane", note="x", amount=0.01)),
    ("venmo", "  Jane Doe   paid  you  $25.00  for  spaced note  ", "body",
     "income", dict(counterparty="Jane Doe", note="spaced note", amount=25.0)),
    ("venmo", "You paid Pet Supplies Plus $43.21 for litter", "You paid Pet Supplies Plus $43.21",
     "expense", dict(counterparty="Pet Supplies Plus", note="litter", amount=43.21,
                     category="Food")),
    ("venmo", "You paid Vet Clinic $200", "You paid Vet Clinic $200",
     "expense", dict(counterparty="Vet Clinic", note="", amount=200.0, category="Vet Bills")),
    ("venmo", "Your Venmo statement is ready", "statement body", "ignore", None),
    ("venmo", "", "", "ignore", None),

    # ---- dues are handed off, not booked ----
    ("venmo", "Jane Doe paid you $15.00 for dues", "dues payment", "dues", None),
    ("venmo", "Someone paid you $10.00 for membership dues", "membership dues", "dues", None),
    ("cashapp", "Alice paid you $15.00 for dues", "dues", "dues", None),

    # ---- Cash App: subject or body, plus merchant spending ----
    ("cashapp", "Alice paid you $30.00 for vet bill", "Alice paid you $30.00",
     "income", dict(counterparty="Alice", note="vet bill", amount=30.0)),
    ("cashapp", "Payment received", "You were sent $12.34 by Carol Jones for food",
     "income", dict(counterparty="Carol Jones", note="food", amount=12.34,
                    category="Foods/Goods fundraisers")),
    ("cashapp", "Payment received", "You were sent $12.34 by Carol Jones. Thanks",
     "income", dict(counterparty="Carol Jones", note="", amount=12.34, message_blank=True)),
    ("cashapp", "You sent $18.00 to Pet Store for supplies", "You sent $18.00 to Pet Store",
     "expense", dict(counterparty="Pet Store", note="supplies", amount=18.0,
                     category="Supplies")),
    ("cashapp", "Receipt", "You paid Corner Vet $75.00 for a checkup.",
     "expense", dict(counterparty="Corner Vet", note="a checkup", amount=75.0,
                     category="Vet Bills")),
    ("cashapp", "Receipt", "You sent $22.00 to Feed Store for kibble.",
     "expense", dict(counterparty="Feed Store", note="kibble", amount=22.0, category="Food")),
    ("cashapp", "Cash App receipt", "You spent $9.99 at CHEWY.COM\nthanks",
     "expense", dict(counterparty="CHEWY.COM", note="", amount=9.99)),
    ("cashapp", "You sent $5 to Bob", "You sent $5 to Bob",
     "expense", dict(counterparty="Bob", note="", amount=5.0, category="Misc/Reimbursement")),
    ("cashapp", "Nothing relevant here", "no amounts at all", "ignore", None),

    # ---- PayPal: several subject and receipt shapes ----
    ("paypal", "You've got money", "Dana Wells sent you $40.00\nThanks!",
     "income", dict(counterparty="Dana Wells", note="", amount=40.0, category="Donations")),
    ("paypal", "Money received", "Money received from Erin Park $60.00",
     "income", dict(counterparty="Erin Park", note="", amount=60.0, category="Donations")),
    ("paypal", "You've got money", "no parseable line here", "ignore", None),
    ("paypal", "Receipt for your payment", "You sent a $99.00 usd payment to VET SERVICES LLC",
     "expense", dict(counterparty="VET SERVICES LLC", amount=99.0, category="Vet Bills")),
    ("paypal", "Receipt", "You sent $12.00 payment to Litter Co",
     "expense", dict(counterparty="Litter Co", amount=12.0, category="Food")),
    ("paypal", "Your monthly statement is ready", "statement", "ignore", None),
    ("paypal", "Frank Moore: $20.00 usd", "compact style",
     "income", dict(counterparty="Frank Moore", amount=20.0)),
    ("paypal", "Grace Hill: $7.50", "compact style",
     "income", dict(counterparty="Grace Hill", amount=7.5)),
    ("paypal", "Henry Ito sent you $33.00", "subject style",
     "income", dict(counterparty="Henry Ito", amount=33.0)),
    ("paypal", "unrelated subject", "unrelated body", "ignore", None),
]

CLASSIFIERS = {
    "venmo": finance._classify_venmo,
    "cashapp": finance._classify_cashapp,
    "paypal": finance._classify_paypal,
}

#(from address, subject, body, expected status, expected provider or None)
DISPATCH_CASES = [
    ("venmo@venmo.com", "Jane paid you $5", "", "income", "venmo"),
    #The merchant-spending pattern reads the body; the subject only decides
    #which provider the mail came from, so a subject-only "You spent" reaches
    #the Cash App classifier and is then ignored.
    ("cash@square.com", "Cash App receipt", "You spent $9.99 at CHEWY.COM", "expense", "cashapp"),
    ("cash@square.com", "You spent $9 at CHEWY", "", "ignore", None),
    ("service@paypal.com", "You've got money", "Dana sent you $5.00", "income", "paypal"),
    #An unknown sender is never classified, however payment-like it reads.
    ("someone@example.com", "Jane paid you $5", "", "unsupported", None),
    #A known sender with no payment wording is not classified either.
    ("venmo@venmo.com", "Your statement is ready", "", "unsupported", None),
]


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def email_for(provider: str, subject: str, body: str) -> Dict[str, Any]:
    senders = {"venmo": "venmo@venmo.com", "cashapp": "cash@square.com",
               "paypal": "service@paypal.com"}
    return {
        "id": "e1",
        "from": senders[provider],
        "subject": subject,
        "content": body,
        "ts_received": "2026-05-01T12:00:00+00:00",
        "message_id": f"<{provider}-{abs(hash((subject, body))) % 10**8}@mail>",
    }


def main() -> int:
    print("=" * 70)
    print("payment email classification regression tests")
    print("=" * 70)

    print(f"\n[1] each provider pattern extracts the right event ({len(CASES)} cases)")
    for provider, subject, body, want_status, want_fields in CASES:
        event, status = CLASSIFIERS[provider](email_for(provider, subject, body))
        label = f"{provider}: {subject[:42]!r}"
        if status != want_status:
            check(label, want_status, status)
            continue
        if want_fields is None:
            check(f"{label} -> {status}", None, event)
            continue
        if event is None:
            check(label, want_fields, None)
            continue
        actual = {k: getattr(event, k) for k in want_fields}
        if actual != want_fields:
            check(label, want_fields, actual)
            continue
        check(f"{label} -> {status}", provider, event.provider)

    print("\n[2] shared fields come off the email, not the pattern")
    event, _status = finance._classify_venmo(
        email_for("venmo", "Jane paid you $5.00 for food", "body text")
    )
    check("email id carried through", "e1", event.email_id)
    check("raw subject kept", "Jane paid you $5.00 for food", event.raw_subject)
    check("raw content kept", "body text", event.raw_content)
    check("timestamp parsed as UTC", "2026-05-01 12:00:00+00:00", str(event.ts))

    print("\n[3] the dispatcher routes by sender and content")
    for sender, subject, body, want_status, want_provider in DISPATCH_CASES:
        email = {"id": "d", "from": sender, "subject": subject, "content": body,
                 "ts_received": "2026-05-01T12:00:00+00:00", "message_id": "<d@mail>"}
        event, status = finance._classify_email(email)
        label = f"{sender} / {subject[:30]!r}"
        check(f"{label} -> {want_status}", want_status, status)
        check(f"{label} provider", want_provider, event.provider if event else None)

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
