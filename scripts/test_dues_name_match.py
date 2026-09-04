"""Regression tests for dues payer-name extraction and match gating.

The incident: "Hi I paid $15 on Cash App, my name is Kynslee Guthrie" parsed to
a name of "Hi I paid $15 on Cash App", because the extractor had no rule for
"my name is X" and fell through to a comma split that took everything BEFORE
the name. The leading "Hi" then fuzzy-matched a leftover 2024 test row at 0.10
confidence, and since nothing floored the sheet score and nothing compared the
sheet row against the receipt, the payment auto-verified onto that test row.

Every message below is a real one from logs/dues on production.

Run:  python scripts/test_dues_name_match.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.handlers.dues import (  # noqa: E402
    _extract_payer_name,
    _payer_agrees,
    _score_sheet,
    _MIN_SHEET_SCORE,
)

FAILURES: list[str] = []


def _ascii(s: str) -> str:
    """Windows consoles default to cp1252; smart quotes and emoji crash them."""
    return (s or "").encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", _ascii(name), "" if ok else "  -> " + _ascii(detail)))
    if not ok:
        FAILURES.append(_ascii(name))


def expect_name(text: str, want, author: str = "") -> None:
    got = _extract_payer_name(text, author_keys=(author, author))
    check("%r" % text[:52], got == want, "got %r, want %r" % (got, want))


def test_self_identification():
    print("\n[1] people who say their own name (the reported defect)")
    expect_name("Hi I paid $15 on Cash App, my name is Kynslee Guthrie", "Kynslee Guthrie")
    expect_name("Did this a couple days ago, but my name is Grant Atkinson. I payed $30 via PayPal.",
                "Grant Atkinson")
    expect_name("I just paid dues in cash to Enoch at the activity fair, I’m Daniel Graves",
                "Daniel Graves")
    expect_name("paid $20 cash to megan at the activity fair i’m tayler carter", "tayler carter")
    expect_name("Hi! I’m Katherine Le and I paid my dues via paypal", "Katherine Le")
    expect_name("my names joshua moro and i paid $15 via cashapp! cash tag $papipasta1", "joshua moro")
    expect_name("Hello! I paid my dues! It’s under Addison Grace Adams and I paid on PayPal",
                "Addison Grace Adams")
    expect_name("My name is Carla Serrano I paid through cash-app.", "Carla Serrano")
    expect_name("paid $15 via CashApp as Mintea Fresh", "Mintea Fresh")


def test_leading_name():
    print("\n[2] name first, provider after")
    expect_name("Bunny Wood venmo", "Bunny Wood")
    expect_name("Nicholas Ho - Paypal", "Nicholas Ho")
    expect_name("Alysa Potter paypal", "Alysa Potter")
    expect_name("Mia Perez $15 via venmo :3", "Mia Perez")
    expect_name("Daniela Del Angel Payed cash to Megan", "Daniela Del Angel")
    expect_name("Kai Waddell & cashapp with cash tag $KaiW1738", "Kai Waddell")
    expect_name("Bri Rogers via CashApp [Brinrogers11]", "Bri Rogers")

    #Surname left uncapitalized, rescued by the payment word right after it.
    expect_name("Celeste bowles paid $15 via venmo", "Celeste bowles")
    #"under X" without the "it's" lead-in.
    expect_name("Paid dues on PayPal under Giovanna Hernandez!", "Giovanna Hernandez")

    print("\n[3] the provider is not the payer")
    #Old parser took the capitalized word after the paid-phrase, yielding
    #"Venmo" and "Paypal" as people's names.
    expect_name("Jon Ruhl - paid via Venmo from @jonruhlmusic", "Jon Ruhl")
    expect_name("Mai Thy Doan - paid via Paypal", "Mai Thy Doan")

    print("\n[4] a handle in parentheses is not the payer")
    #Old parser preferred parentheses above everything else.
    expect_name("Charlotte Brownlee (phat_cat_207) paypal", "Charlotte Brownlee", author="phat_cat_207")
    expect_name("Sophia Mason paid on Venmo(Pamela-Mason-24)", "Sophia Mason")
    expect_name("Diana Villegas via CashApp (@ dianaiceflame)", "Diana Villegas", author="dianaiceflame")
    expect_name("Jaycee Shelton paid on CashApp (jcshelton25)", "Jaycee Shelton")
    expect_name("Steffani Carrington-Casimir ($SteffiCarr), Cashapp", "Steffani Carrington-Casimir")


def test_trailing_name():
    print("\n[5] name tacked on the end (uncommon, still handled)")
    expect_name("Paid 20$ on PayPal - Logan Hackett", "Logan Hackett")
    expect_name("I paid $15 on PayPal -Alazjah Tates", "Alazjah Tates")
    expect_name("I paid $15 on Venmo! Lilian Thomson", "Lilian Thomson")
    expect_name("I payed $15 cash to Megan. Adolfo Lopez", "Adolfo Lopez")
    #The treasurer collecting the money is not the payer.
    expect_name("$15 cash paid to Megan; Sumehra Rahman", "Sumehra Rahman")


def test_refuses_junk():
    print("\n[6] no name present -> None, never a clause")
    for text in ("Payed cash to megan", "I paid cash to Izzy", "AegisVentus Paypal",
                 "$kukiearii cashapp", "$lizcoulter1, paid on cashapp",
                 "Bryan Huynh - Venmo *god damn robots*"):
        got = _extract_payer_name(text)
        bad = got is not None and (len(got.split()) > 4 or "paid" in got.lower())
        check("no clause from %r" % text[:34], not bad, "got %r" % got)
    expect_name("Payed cash to megan", None)
    expect_name("I paid cash to Izzy\U0001f60a", None)
    expect_name("(Sorry for being late, the last time I checked I didn’t have access "
                "yet to this channel.) $freshmintea", None)
    #A sentence that merely opens with a capitalized word is not a name. This is
    #the risk the loose leading rule takes on, so it is pinned here.
    expect_name("Kayla asked me to send this in for her", None)
    expect_name("Sent money for dues", None)


def test_sheet_floor():
    print("\n[7] a name sliver no longer matches a stray row")
    #The exact 2024 test row the payment landed on.
    junk = {"full_name": "Hi", "discord_username": "austinbaustinb",
            "paid_where": "Paypal", "kind": "$15 Donation, Discord Verification"}
    parsed = {"author_name": "kyns3029", "author_display": "Kynspee",
              "name": "Kynslee Guthrie", "provider": "cashapp"}
    check("junk row scores below the floor",
          _score_sheet(parsed, junk) < _MIN_SHEET_SCORE,
          "scored %.3f, floor %.2f" % (_score_sheet(parsed, junk), _MIN_SHEET_SCORE))

    real = {"full_name": "Kynslee Guthrie", "discord_username": "kyns3029",
            "paid_where": "Cashapp", "kind": "$15 Dues"}
    check("the real row still clears the floor",
          _score_sheet(parsed, real) >= _MIN_SHEET_SCORE,
          "scored %.3f" % _score_sheet(parsed, real))

    #A row that shares only the provider word must not count as a match.
    provider_only = {"full_name": "Someone Else", "discord_username": "nobody",
                     "paid_where": "Cashapp", "kind": ""}
    check("provider-word-only row is not a match",
          _score_sheet(parsed, provider_only) < _MIN_SHEET_SCORE,
          "scored %.3f" % _score_sheet(parsed, provider_only))


def test_payer_agreement():
    print("\n[8] the receipt has to name the same person as the sheet row")
    #Wrong attachments seen in production.
    for sheet, payer in (("Hi", "Kynslee Guthrie"),
                         ("Tahia Tahsin Hanif", "Diana Villegas"),
                         ("Grant Atkinson", "Mai Thy Doan"),
                         ("Sam Locke", "Isabella Tellez")):
        check("reject %s <- %s" % (sheet, payer), not _payer_agrees(sheet, payer))

    print("\n[9] relatives and spelling drift still agree")
    for sheet, payer in (("Bunny Wood", "Bunny Wood"),
                         ("Kai Waddell", "Lindsay Waddell"),
                         ("Sophia Mason", "Pamela Mason"),
                         ("Emma Boyd", "Echo Boyd"),
                         ("Celeste Bowes", "Celeste Bowles"),
                         ("Addison Grace Adams", "Addison Adams"),
                         ("Edgar Herrera", "Edgar"),
                         ("Nadia Sanchez", "Nadia S"),
                         ("Alazjah N Tates", "Alazjah Tates")):
        check("accept %s <- %s" % (sheet, payer), _payer_agrees(sheet, payer))

    check("a missing payer name falls back to other evidence",
          _payer_agrees("Bunny Wood", ""))


def main() -> int:
    print("=" * 70)
    print("dues payer-name extraction / match gating")
    print("=" * 70)
    test_self_identification()
    test_leading_name()
    test_trailing_name()
    test_refuses_junk()
    test_sheet_floor()
    test_payer_agreement()
    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d):" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
