"""Regression test for spam-rule false positives on ordinary links.

The bug this pins down: PHONE_RE had no boundaries, so it matched any 10-digit
window inside a longer digit run. Discord CDN links carry two 18-19 digit
snowflakes in the path, which read as a "phone number" (+2) and stacked with the
new-join URL signal (+2) to clear MIN_SPAM_SCORE. A member who joined last week
and pasted a cat gif got queued for a ban vote.

Real phone numbers and real scam text must still score exactly as before.

Run:  python scripts/test_spam_false_positives.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.spam import (  # noqa: E402
    MIN_SPAM_SCORE, PHONE_RE, check_spam,
)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


class _Role:
    name = "member"


class _Author:
    """An untrusted, recently-joined member -- the worst case for false positives."""

    id = 609961793087078410
    name = "test-member"
    bot = False
    roles = [_Role()]

    def __init__(self, days_ago: int = 1) -> None:
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=days_ago)


class _Guild:
    id = 1


class _Channel:
    id = 2


class _Message:
    id = 3

    def __init__(self, content: str, days_ago: int = 1) -> None:
        self.content = content
        self.author = _Author(days_ago)
        self.guild = _Guild()
        self.channel = _Channel()


class _Settings:
    trusted_role_names = ["officer"]
    spam_trust_message_threshold = 50
    officer_role_id = None
    officer_role_ids: list[int] = []


def score_of(content: str, days_ago: int = 1) -> tuple[bool, str, int]:
    # Each call needs a distinct author id, or the module-level message counter
    # trips the trusted_message_count gate partway through the suite.
    msg = _Message(content, days_ago)
    msg.author.id = _Author.id + len(FAILURES) + hash(content) % 100000
    return check_spam(msg, _Settings())


#The exact link shape from the reported false positive, plus neighbours.
BENIGN_LINKS = [
    "https://cdn.discordapp.com/attachments/1084372918273645/1180293847562819/togif.gif",
    "https://media.discordapp.net/attachments/900123456789012345/901234567890123456/cat.png?ex=66f0a1&is=66ef4f&hm=ab",
    "https://tenor.com/view/cat-kitten-cute-togif-gif-25839471",
    "https://www.youtube.com/watch?v=1234567890123456789",
    "look at this https://cdn.discordapp.com/attachments/1084372918273645/1180293847562819/togif.gif",
]

#Digit runs that are not phone numbers.
NON_PHONE = [
    "1084372918273645",          # snowflake
    "order 12345678901234",      # long id
    "1234567890123",             # 13 digits
]

#Must still be caught.
REAL_PHONE = [
    "call me at 555-123-4567",
    "my number is (555) 123-4567",
    "reach me +1 5551234567",
    "text 555.123.4567 for details",
]

#End-to-end scam text that must stay above the bar.
REAL_SPAM = [
    "Moving abroad, giving away my items for free, DM me if interested",
    "Selling my season tickets, first come first serve, message me if interested",
]


def main() -> int:
    print("Benign links must not be flagged:")
    for link in BENIGN_LINKS:
        flagged, reason, score = score_of(link)
        check(link[:64], not flagged, "flagged reason=%s score=%d" % (reason, score))

    print("\nLong digit runs are not phone numbers:")
    for text in NON_PHONE:
        check(text, PHONE_RE.search(text) is None, "matched %r" % text)

    print("\nReal phone numbers still match:")
    for text in REAL_PHONE:
        check(text, PHONE_RE.search(text) is not None, "no match")

    print("\nReal spam still scores at or above MIN_SPAM_SCORE (%d):" % MIN_SPAM_SCORE)
    for text in REAL_SPAM:
        flagged, reason, score = score_of(text)
        check(text[:64], flagged, "not flagged, reason=%s score=%d" % (reason, score))

    print("\nA phone number in prose beside a link still counts:")
    flagged, reason, score = score_of(
        "pics here https://cdn.discordapp.com/attachments/1084372918273645/1180293847562819/x.gif "
        "or text me at 555-123-4567"
    )
    check("prose phone survives URL stripping", score >= 4,
          "score=%d reason=%s" % (score, reason))

    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
