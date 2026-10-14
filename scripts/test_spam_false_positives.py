"""Regression tests for spam giveaways and false positives on ordinary posts.

The bug this pins down: PHONE_RE had no boundaries, so it matched any 10-digit
window inside a longer digit run. Discord CDN links carry two 18-19 digit
snowflakes in the path, which read as a "phone number" (+2) and stacked with the
new-join URL signal (+2) to clear MIN_SPAM_SCORE. A member who joined last week
and pasted a cat gif got queued for a ban vote.

Also covers a camera giveaway that evaded the device and DM wording rules.

Run:  python scripts/test_spam_false_positives.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#Sends this process's machine log to a scratch directory, so the tests do
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

from tomcat.spam import (  # noqa: E402
    HIGH_CONFIDENCE_SPAM_SCORE, MIN_SPAM_SCORE, PHONE_RE, check_spam,
)

FAILURES: list[str] = []
AUTHOR_IDS = count(609961793087078410)


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
    msg.author.id = next(AUTHOR_IDS)
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

#Exact message transcribed from the reported camera giveaway.
CAMERA_GIVEAWAY = """If you are in need of a camera, I am offering my Sony A7 III with the Sony 18-105mm f/4 lens as a giveaway.

Both items come in their original boxes and are in nearly brand-new condition. I purchased them in December 2024, used them only for a few test shots, and then stored them carefully.

I am passing them on because I recently purchased a drone, and I would prefer to give this camera to someone who genuinely needs one but may not be able to afford it. This is strictly first come, first served. No holds or partial reservations.

Please send me a direct message if you are interested."""

GIVEAWAY_VARIANTS = [
    "I am offering my Sony camera as a giveaway. Send me a direct message.",
    "I have a camera and am passing it on to someone. Send me a private message.",
    "I am passing them on to someone who needs a camera. Send me a direct message.",
    "I would like to give this camera to someone who needs it. Send me a private message.",
    "Giving away my laptop. Send me a message.",
]

BENIGN_CAMERA_POSTS = [
    "Does anyone have experience with the Sony A7 III camera and 18-105mm lens?",
    "The camera club is offering a workshop on using a Sony camera.",
    "Please give this camera a try before buying it.",
    "Sony A7 III camera for sale, $800, pickup on campus this afternoon.",
    "I entered a camera giveaway yesterday and won a lens!",
]


def check_giveaways() -> None:
    flagged, reason, score = score_of(CAMERA_GIVEAWAY)
    check("reported camera giveaway clears auto-delete threshold",
          flagged and score >= HIGH_CONFIDENCE_SPAM_SCORE,
          "flagged=%s reason=%s score=%d" % (flagged, reason, score))
    for text in GIVEAWAY_VARIANTS:
        flagged, reason, score = score_of(text)
        check(text[:64], flagged and score >= HIGH_CONFIDENCE_SPAM_SCORE,
              "flagged=%s reason=%s score=%d" % (flagged, reason, score))

    #A DM request alone must remain below the auto-delete threshold. Only the
    #recent member gets the behavioral bonus; established members aren't flagged.
    for kind in ("direct", "private"):
        text = "Please send me a %s message about tomorrow's meeting." % kind
        flagged, reason, recent_score = score_of(text)
        check("recent member's %s message request is alert-only" % kind,
              flagged and recent_score < HIGH_CONFIDENCE_SPAM_SCORE,
              "flagged=%s score=%d" % (flagged, recent_score))
        flagged, reason, old_score = score_of(text, days_ago=30)
        check("established member's %s message request isn't flagged" % kind,
              not flagged and recent_score - old_score == 2,
              "flagged=%s recent=%d old=%d" % (flagged, recent_score, old_score))

    #An urgency phrase by itself must not auto-delete ordinary event posts.
    for phrase in ("first come first serve", "first come, first served", "first come; first served"):
        flagged, reason, score = score_of("Seats for the club dinner are " + phrase)
        check(phrase + " contributes urgency without auto-deletion",
              2 <= score < HIGH_CONFIDENCE_SPAM_SCORE, "score=%d" % score)

    for text in BENIGN_CAMERA_POSTS:
        flagged, reason, score = score_of(text)
        check(text[:64], not flagged, "flagged reason=%s score=%d" % (reason, score))
    sale = "Selling my Sony camera for $800. Send me a private message to arrange pickup."
    flagged, reason, score = score_of(sale)
    check("recent member's ordinary camera sale isn't auto-deleted",
          score < HIGH_CONFIDENCE_SPAM_SCORE, "reason=%s score=%d" % (reason, score))
    flagged, reason, score = score_of(sale, days_ago=30)
    check("established member's ordinary camera sale isn't flagged", not flagged,
          "reason=%s score=%d" % (reason, score))

    trusted = _Message(CAMERA_GIVEAWAY)
    trusted.author.id = next(AUTHOR_IDS)
    role = _Role()
    role.name = "officer"
    trusted.author.roles = [role]
    check("trusted role exemption still applies",
          check_spam(trusted, _Settings()) == (False, "trusted_role", 0))


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

    print("\nCamera giveaways and ordinary camera posts:")
    check_giveaways()
    print("\nCamera rules must also work with the no-rapidfuzz fallback:")
    with patch("tomcat.spam._fuzzy_hit", side_effect=lambda text, phrase, thresh: phrase.lower() in text.lower()):
        check_giveaways()

    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
