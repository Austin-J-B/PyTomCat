"""Regression tests for PayPal payer extraction and the pending-dues clock.

Both cover the same incident: 22 dues payments of exactly $15 were reclassified
as donations over spring 2026 while Gmail auth was expiring weekly, and four of
them were unattributable because the payer name read "You've".

Run:  python scripts/test_pending_dues.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.utils.payments import extract_paypal_payer  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


GOT_MONEY = "You've got money"


def body(name: str, amount: str = "15.00", note: str | None = None) -> str:
    out = ("\n\n\nYou've got money\n\n\nCampus Cat Coalition, you received $%s\xa0USD\n\n"
           "Hello, Campus Cat Coalition\n\n\n%s sent you $%s\xa0USD\n\n" % (amount, name, amount))
    if note:
        out += "Note from %s:\n\n%s\n\n" % (name, note)
    out += "Transaction ID\n4VW57989RN9610812\n"
    return out


def test_payer_extraction():
    print("\n[1] PayPal payer comes from the body, never the subject")
    check("plain notification", extract_paypal_payer(GOT_MONEY, body("Miralee Martinez")) == "Miralee Martinez",
          repr(extract_paypal_payer(GOT_MONEY, body("Miralee Martinez"))))
    check("notification with a note",
          extract_paypal_payer(GOT_MONEY, body("Aegis Evans", note="Heard Yall need Summer Feeders")) == "Aegis Evans")
    check("trailing colon stripped",
          extract_paypal_payer(GOT_MONEY, "Note from Jacob Sandoval:\n\nBake sale\n") == "Jacob Sandoval")
    check("'Money received from' layout",
          extract_paypal_payer(GOT_MONEY, "Money received from Cora Bell $15.00\n") == "Cora Bell")

    print("\n[2] junk is refused rather than invented")
    #The exact defect: the subject is always "You've got money", so a
    #subject-first fallback attributed four real payments to "You've".
    for bad_body in ("", "no useful content here", "You've got money\n"):
        got = extract_paypal_payer(GOT_MONEY, bad_body)
        check("no name -> None (body=%r)" % bad_body[:22], got is None, repr(got))
    check("'You've' never returned",
          extract_paypal_payer(GOT_MONEY, "You've got money") != "You've")
    check("subject naming a payer is still honoured",
          extract_paypal_payer("Kyle Moore sent you $15.00 USD", "") == "Kyle Moore",
          repr(extract_paypal_payer("Kyle Moore sent you $15.00 USD", "")))


def test_pending_clock():
    print("\n[3] pending clock pauses when the roster is unreadable")
    from tomcat.handlers import finance as f
    from tomcat.handlers import dues as d

    check("dues exposes an authoritative-load flag",
          callable(getattr(d, "membership_data_is_authoritative", None)))
    ok, _detail = d.membership_data_is_authoritative()
    check("flag returns (bool, str)", isinstance(ok, bool))

    import inspect
    src = inspect.getsource(f._process_pending_dues)
    check("expiry is gated on corroboration availability",
          "corroboration_ok" in src and "if not corroboration_ok:" in src)
    check("pause happens before anything expires",
          src.index("pending_dues_clock_paused") < src.index("if now >= expires"))
    check("grace window is positive", f._PENDING_CLOCK_GRACE_HOURS > 0,
          str(f._PENDING_CLOCK_GRACE_HOURS))

    # The clock must be pushed out, not reset, so a long outage does not
    # translate into a proportionally long wait afterwards.
    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=40)
    pushed = now + timedelta(hours=f._PENDING_CLOCK_GRACE_HOURS)
    check("a 40-day-stale expiry moves to now+grace, not now+40d",
          (pushed - now) == timedelta(hours=f._PENDING_CLOCK_GRACE_HOURS)
          and pushed > now and stale < now)


def main() -> int:
    print("=" * 70)
    print("pending dues / paypal payer tests")
    print("=" * 70)
    test_payer_extraction()
    test_pending_clock()
    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
