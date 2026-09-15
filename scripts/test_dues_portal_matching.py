"""Dues portal regression: posts that stayed in the portal in September 2026.

Three separate faults left payments unprocessed:

  - Every analysed post went into logs/dues/index.jsonl, and the portal fetch
    skipped anything listed there. Posts first scanned while the roster sheet
    404'd were matched to nothing and then never scanned again.
  - A name match plus the provider on the form scores 0.7 + 0.2, which is
    0.8999999999999999 in floating point and missed the 0.90 cutoff.
  - Cash handed to an officer names no provider, so the post was never scored
    at all. An officer replying "confirming" is the evidence there, and the
    bot had no rule for it.

Run: python scripts/test_dues_portal_matching.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Sends this process's machine log to a scratch directory, so the test does
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

from tomcat.handlers import dues

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


NOW = datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)
CUR_SEM = "Fall 2026"


class FakeAuthor:
    def __init__(self, uid: int, name: str, display: Optional[str] = None, officer: bool = False):
        self.id = uid
        self.name = name
        self.display_name = display or name
        self.global_name = display or name
        self.officer = officer


class FakeReference:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeMessage:
    def __init__(self, mid: int, content: str, author: FakeAuthor, minutes_ago: float,
                 reply_to: Optional[int] = None):
        self.id = mid
        self.content = content
        self.author = author
        self.created_at = NOW - timedelta(minutes=minutes_ago)
        self.reference = FakeReference(reply_to) if reply_to else None
        self.reactions: List[Any] = []


def row(name: str, handle: str, email: str, *, semester: str = CUR_SEM,
        verified: bool = False, paid_where: str = "Cash") -> Dict[str, Any]:
    return {"full_name": name, "discord_username": handle, "email": email,
            "semester": semester, "verified": verified, "paid_where": paid_where}


#Officer status comes from the author, so the tests need no guild or roles.
dues.is_officer = lambda member, _settings: bool(getattr(member, "officer", False))

JASMINE = FakeAuthor(1, "jasminek1211", "JasmineK")
ATLAS = FakeAuthor(2, "atlas", "atlas[cat] [records]", officer=True)
CHLOE = FakeAuthor(3, "shikeiri", "Chloe")
DEREK = FakeAuthor(4, "a1darek", "derek [Head of TNR]", officer=True)
ACACIA = FakeAuthor(5, "acaciuscat", "AcaciusCat")
JEANETT = FakeAuthor(6, "jeanettzapata", "Jeanett Zapata")

ROWS = [
    row("Jasmine Kasper", "JasmineK1211", "jmk1286@mavs.uta.edu"),
    row("Chloe Samayoa-Garcia", "shikeiri", "cms4996@mavs.uta.edu", paid_where=""),
    row("Acacia Shiroma", "spitonme", "acs7801@mavs.uta.edu"),
    row("Jeanett Zapata", "JeanettZapata", "jxz2126@mavs.uta.edu", paid_where=""),
    #A past semester's row must never satisfy a confirmation.
    row("Acacia Shiroma", "spitonme", "acs7801@mavs.uta.edu", semester="Spring 2026"),
]


def test_officer_confirmations() -> None:
    print("\nofficer confirmations")
    msgs = [
        #Cash to an officer, confirmed by that officer in the next post.
        FakeMessage(10, "My name is Jasmine Kasper I have Atlas $15", JASMINE, 300),
        FakeMessage(11, "confirming :)", ATLAS, 297),
        #An in-kind donation, confirmed with a reply that is not adjacent.
        FakeMessage(12, "Chloe Samayoa - donated 2 towels (:", CHLOE, 200),
        FakeMessage(13, "Acacia Shiroma $15 Izzy", ACACIA, 150),
        FakeMessage(14, "confirming!", DEREK, 120, reply_to=12),
        #No officer said anything about this one; it must stay.
        FakeMessage(15, "Jeanett Zapata, gave 2 towels to Megan", JEANETT, 60),
    ]
    got = dues._officer_confirmed_payments(msgs, ROWS, CUR_SEM)
    by_msg = {int(c["message"].id): c for c in got}
    check("adjacent confirmation matches Jasmine's form row",
          "jmk1286@mavs.uta.edu", (by_msg.get(10) or {}).get("row", {}).get("email"))
    check("reply confirmation matches Chloe despite the hyphenated surname",
          "cms4996@mavs.uta.edu", (by_msg.get(12) or {}).get("row", {}).get("email"))
    check("confirmation message recorded for deletion",
          14, int(getattr((by_msg.get(12) or {}).get("confirmation"), "id", 0)))
    check("unconfirmed posts are left alone", {10, 12}, set(by_msg))


def test_confirmation_guards() -> None:
    print("\nconfirmation guards")
    #A non-officer saying "confirmed" is not evidence.
    msgs = [
        FakeMessage(20, "Acacia Shiroma $15 Izzy", ACACIA, 30),
        FakeMessage(21, "confirmed", JEANETT, 29),
    ]
    check("non-officer confirmation ignored", [], dues._officer_confirmed_payments(msgs, ROWS, CUR_SEM))

    #A long officer message that happens to contain the word is conversation.
    msgs = [
        FakeMessage(22, "Acacia Shiroma $15 Izzy", ACACIA, 30),
        FakeMessage(23, "can someone confirm whether the bake sale money went to Izzy or to the cash box "
                        "because I have no record of it", ATLAS, 29),
    ]
    check("long officer message ignored", [], dues._officer_confirmed_payments(msgs, ROWS, CUR_SEM))

    #No current-semester form row: stays in the portal for a human.
    stale = [row("Acacia Shiroma", "spitonme", "acs7801@mavs.uta.edu", semester="Spring 2026")]
    msgs = [
        FakeMessage(24, "Acacia Shiroma $15 Izzy", ACACIA, 30),
        FakeMessage(25, "confirmed", ATLAS, 29),
    ]
    check("stale-semester row is not verified", [], dues._officer_confirmed_payments(msgs, stale, CUR_SEM))

    #An already-verified row has nothing left to do.
    done = [row("Acacia Shiroma", "spitonme", "acs7801@mavs.uta.edu", verified=True)]
    check("already-verified row skipped", [], dues._officer_confirmed_payments(msgs, done, CUR_SEM))

    #An unrelated adjacent post by an officer is not a payment to confirm.
    msgs = [
        FakeMessage(26, "Megan - cashapp", FakeAuthor(7, "m_digio", "Megan", officer=True), 30),
        FakeMessage(27, "confirmed", ATLAS, 29),
    ]
    megan = [row("Megan Digiovanni", "m_digio", "med3041@mavs.uta.edu")]
    check("officer's own post is not confirmed by adjacency", [],
          dues._officer_confirmed_payments(msgs, megan, CUR_SEM))
    #... but an explicit reply to it is.
    msgs[1] = FakeMessage(27, "confirmed", ATLAS, 29, reply_to=26)
    got = dues._officer_confirmed_payments(msgs, megan, CUR_SEM)
    check("explicit reply confirms an officer's own payment", [26], [int(c["message"].id) for c in got])

    #Confirmations that arrive days later are too loose to trust by adjacency.
    msgs = [
        FakeMessage(28, "Acacia Shiroma $15 Izzy", ACACIA, 60 * 24 * 5),
        FakeMessage(29, "confirmed", ATLAS, 10),
    ]
    check("adjacent confirmation outside the window ignored", [],
          dues._officer_confirmed_payments(msgs, ROWS, CUR_SEM))

    #Two different members both fit the post: do not guess.
    twins = [row("Jasmine Kasper", "JasmineK1211", "a@x.edu"), row("Jasmine Kasper", "jk2", "b@x.edu")]
    msgs = [
        FakeMessage(30, "My name is Jasmine Kasper I have Atlas $15", FakeAuthor(8, "someoneelse"), 30),
        FakeMessage(31, "confirmed", ATLAS, 29),
    ]
    check("ambiguous match left for a human", [], dues._officer_confirmed_payments(msgs, twins, CUR_SEM))


def test_current_semester_wins_ties() -> None:
    print("\nsheet match tie-break")
    #Derek Fuentes filled out the form in Spring and again in Fall with the same
    #handle and provider. Spring rows stay live until Sept 15, so both score
    #1.40; the older row came first in the sheet and the job then skipped him
    #as a stale semester.
    spring = row("Derek Fuentes", "a1darek", "dsf1084@mavs.uta.edu", semester="Spring 2026", paid_where="Paypal")
    fall = row("Derek Fuentes", "a1darek", "dsf1084@mavs.uta.edu", semester=CUR_SEM, paid_where="Paypal")
    ranked = dues._rank_sheet_matches([(1.4, spring), (1.4, fall)], CUR_SEM)
    check("current-semester row wins a tie", CUR_SEM, ranked[0][1]["semester"])
    ranked = dues._rank_sheet_matches([(1.4, spring), (0.9, fall)], CUR_SEM)
    check("a clearly better older row still wins", "Spring 2026", ranked[0][1]["semester"])


def test_every_lookup_contributes_candidates() -> None:
    print("\ncandidate rows")
    #Christopher Mendoza: his username is on the Spring row only; the Fall row
    #carries his display name, his full name and the provider he used.
    spring = {"full_name": "Christopher Mendoza", "discord_username": "Pancakemixteamdj ",
              "semester": "Spring 2026", "paid_where": "", "kind": "Food/Litter Donation",
              "email": "cam6723@mavs.uta.edu", "payment_username": "Christopher Mendoza"}
    fall = {"full_name": "Christopher Mendoza", "discord_username": "Pancake(Chris/topher)",
            "semester": CUR_SEM, "paid_where": "Cashapp", "kind": "$15 Donation, Discord Verification",
            "email": "cam6723@mavs.uta.edu", "payment_username": "$ChristopherMendoza24"}
    other = {"full_name": "Someone Else", "discord_username": "unrelated_person",
             "semester": CUR_SEM, "paid_where": "Venmo", "kind": "", "email": "x@x.edu", "payment_username": ""}
    members = [spring, fall, other]
    msg = FakeMessage(50, "Christopher Mendoza - 15$ Cash App",
                      FakeAuthor(11, "pancakemixteamdj", "Pancake(Chris/topher)"), 30)
    p = dues._parse_portal_message(msg)
    cands = dues._member_candidates(p, members, dues._member_indexes(members))
    check("current row is a candidate alongside the username's old row",
          True, spring in cands and fall in cands)
    check("unrelated row is not pulled in", False, other in cands)
    scored = [(dues._score_sheet(p, r), r) for r in cands if dues._score_sheet(p, r) >= dues._MIN_SHEET_SCORE]
    best = dues._rank_sheet_matches(scored, CUR_SEM)[0]
    check("Fall row wins the match", CUR_SEM, best[1]["semester"])
    check("Fall row score clears the cutoff", True, dues._meets_auto_verify(best[0], 0.90))


def test_old_post_is_judged_by_its_own_date() -> None:
    print("\nold posts")
    fall_2026 = {"full_name": "Austin Brown", "discord_username": "austinbaustinb", "semester": CUR_SEM}
    #A February 2024 post: a Fall 2026 row did not exist yet and must not match.
    check("Fall 2026 row is not current for a Feb 2024 post", False,
          dues._member_row_is_current(fall_2026, datetime(2024, 2, 17).date()))
    check("Fall 2026 row is current for a Sep 2026 post", True,
          dues._member_row_is_current(fall_2026, NOW.date()))


class DeletableMessage(FakeMessage):
    def __init__(self, *args, pinned: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.pinned = pinned
        self.deleted = False

    async def delete(self):
        self.deleted = True


async def test_pinned_posts_are_never_touched() -> None:
    print("\npinned posts")
    #The 2024 pinned instructions post names every provider, so it scored as a
    #payment, matched its author's row, and was deleted.
    pinned = DeletableMessage(60, "**Dues are either $15 a semester** - On Paypal - On Venmo - On Cashapp",
                              FakeAuthor(12, "austinbaustinb"), 60 * 24 * 900, pinned=True)
    payment = DeletableMessage(61, "Leslie Morales, $20 Cash App", FakeAuthor(13, "lesmorales11"), 30)

    class Channel(FakeChannel):
        async def fetch_message(self, mid):
            return {60: pinned, 61: payment}[int(mid)]

    channel = Channel([pinned, payment])
    dues.settings.ch_due_portal = 123
    bot = type("Bot", (), {"get_channel": lambda _self, _cid: channel})()
    got = [int(m.id) for m in await dues._fetch_portal_messages(bot)]
    check("pinned post is not fetched for scoring", [61], got)
    got = [int(m.id) for m in await dues._fetch_portal_messages(bot, include_processed=True)]
    check("pinned post is not fetched for cleanup either", [61], got)
    deleted = await dues._delete_portal_messages(bot, [60, 61])
    check("delete skips the pinned post", (1, False, True), (deleted, pinned.deleted, payment.deleted))


def test_consulting_officers_owe_no_dues() -> None:
    print("\ndues-exempt roles")
    role = lambda rid: type("R", (), {"id": rid})()
    consulting = type("M", (), {"roles": [role(1101141660294971413)]})()
    officer = type("M", (), {"roles": [role(845035667661783061)]})()
    saved = getattr(dues.settings, "dues_exempt_role_ids", [])
    try:
        dues.settings.dues_exempt_role_ids = [1101141660294971413]
        check("consulting officer is exempt", True, dues._is_dues_exempt(consulting))
        check("regular officer is not exempt", False, dues._is_dues_exempt(officer))
        dues.settings.dues_exempt_role_ids = []
        check("nobody is exempt when unconfigured", False, dues._is_dues_exempt(consulting))
    finally:
        dues.settings.dues_exempt_role_ids = saved


def test_threshold_rounding() -> None:
    print("\nauto-verify threshold")
    #Name overlap (0.70) plus provider on the form (0.20): the Camila Davila case.
    score = dues._W["sheet"]["name_overlap"] + dues._W["sheet"]["provider_in_sheet"]
    check("0.7 + 0.2 is below 0.90 in raw floats (the bug)", True, score < 0.90)
    check("0.7 + 0.2 meets the 0.90 cutoff", True, dues._meets_auto_verify(score, 0.90))
    check("0.85 still misses the cutoff", False, dues._meets_auto_verify(0.85, 0.90))


class FakeChannel:
    def __init__(self, messages: List[FakeMessage]):
        self.messages = messages

    def history(self, limit: int, oldest_first: bool):
        async def gen():
            for m in sorted(self.messages, key=lambda m: m.created_at, reverse=not oldest_first)[:limit]:
                yield m
        return gen()


class FakeReaction:
    def __init__(self, emoji: str):
        self.emoji = emoji
        self.me = True


async def test_index_does_not_hide_posts(tmp: Path) -> None:
    print("\nanalysed posts are rescanned")
    dues.DUES_DIR = str(tmp)
    dues.DUES_INDEX = str(tmp / "index.jsonl")

    posted = FakeMessage(40, "Derek Fuentes, 15$ via paypal", FakeAuthor(9, "a1darek"), 30)
    done = FakeMessage(41, "Leslie Morales, $20 Cash App", FakeAuthor(10, "lesmorales11"), 20)
    done.reactions = [FakeReaction(dues._DUES_PROCESSED_EMOJI)]

    #First scan ran against an unreadable roster and logged a match to nothing.
    blind = {"event": "dues_portal_analysis", "message_id": 40, "ts": posted.created_at.isoformat(),
             "primary_member": {}, "score_total": 0.95}
    await dues._append_dues_log(blind)

    dues.settings.ch_due_portal = 123
    bot = type("Bot", (), {"get_channel": lambda _self, _cid: FakeChannel([posted, done])})()
    got = [int(m.id) for m in await dues._fetch_portal_messages(bot)]
    check("post already in the index is fetched again", [40], got)

    #The rescan finds the row; the log must carry that match, or portal cleanup
    #(which looks posts up by matched email) never deletes the post.
    matched = dict(blind, primary_member={"email": "dsf1084@mavs.uta.edu", "semester": CUR_SEM})
    await dues._append_dues_log(matched)
    await dues._append_dues_log(matched)
    records = []
    for p in tmp.glob("*.ndjson"):
        records += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    emails = [(r.get("primary_member") or {}).get("email") for r in records if r.get("message_id") == 40]
    check("new match re-logged once, unchanged match not repeated", [None, "dsf1084@mavs.uta.edu"], emails)
    dues._dues_now = lambda: NOW
    check("cleanup lookup finds the post by its new email", [40],
          dues._dues_log_message_ids_for_emails([("dsf1084@mavs.uta.edu", CUR_SEM)]))


async def main() -> int:
    print("=" * 70)
    print("dues portal matching")
    print("=" * 70)
    test_officer_confirmations()
    test_confirmation_guards()
    test_current_semester_wins_ties()
    test_every_lookup_contributes_candidates()
    test_old_post_is_judged_by_its_own_date()
    test_consulting_officers_owe_no_dues()
    await test_pinned_posts_are_never_touched()
    test_threshold_rounding()
    with tempfile.TemporaryDirectory() as tmp:
        await test_index_does_not_hide_posts(Path(tmp))
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
