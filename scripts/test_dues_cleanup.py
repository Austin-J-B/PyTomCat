"""Dues portal cleanup regression: which posts get deleted, and which survive.

Once a dues payment is verified, its post in the dues portal is removed. Two
entry points do that — one driven by verified emails, one by verified sheet rows
— and they now share _delete_logged_portal_messages and
_delete_portal_messages_by_author. The dangerous direction here is deleting too
much: a payment post outside the backfill window, or one by a member who is not
actually in the target set, must survive.

Run: python scripts/test_dues_cleanup.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


class FakeMessage:
    def __init__(self, mid: int, content: str, author: str, days_ago: float = 1.0,
                 naive_ts: bool = False):
        self.id = mid
        self.content = content
        self.author = type("A", (), {"name": author, "display_name": author})()
        ts = NOW - timedelta(days=days_ago)
        self.created_at = ts.replace(tzinfo=None) if naive_ts else ts


class Harness:
    """Stub out the Discord and dues-log edges; keep the real parsing."""

    def __init__(self, messages: List[FakeMessage], log_ids: Optional[List[int]] = None):
        self.messages = messages
        self.log_ids = log_ids or []
        self.deleted: List[int] = []
        self.fetch_calls = 0

    def install(self) -> None:
        async def fetch(_bot, **_kwargs):
            self.fetch_calls += 1
            return self.messages

        async def delete(_bot, ids):
            self.deleted.extend(ids)
            return len(ids)

        dues._fetch_portal_messages = fetch
        dues._delete_portal_messages = delete
        dues._dues_log_message_ids_for_emails = lambda _pairs: list(self.log_ids)
        dues._dues_now = lambda: NOW
        dues._parse_portal_message = lambda m: {
            "content": m.content,
            "ts": m.created_at,
            "author_name": m.author.name,
            "author_display": m.author.display_name,
        }


def row(email: str, handle: str, *, verified: bool = True, semester: str = "Spring 2026") -> Dict[str, Any]:
    return {"email": email, "discord_username": handle,
            "verified": verified, "semester": semester}


async def main() -> int:
    print("=" * 70)
    print("dues portal cleanup regression tests")
    print("=" * 70)

    print("\n[1] timezone alignment lets aware and naive stamps subtract")
    aware = datetime(2026, 5, 1, tzinfo=timezone.utc)
    naive = datetime(2026, 5, 1)
    check("aware reference keeps a zone", timezone.utc,
          dues._same_awareness(naive, aware).tzinfo)
    check("naive reference drops the zone", None,
          dues._same_awareness(aware, naive).tzinfo)
    check("both aware converts", 0,
          (dues._same_awareness(aware, aware) - aware).days)

    print("\n[2] only payment posts by a target, inside the window, are deleted")
    messages = [
        FakeMessage(1, "alice paid via venmo", "alice_h", days_ago=1),
        FakeMessage(2, "bob paid via venmo", "bob_h", days_ago=1),
        FakeMessage(3, "alice paid via venmo", "alice_h", days_ago=400),
        FakeMessage(4, "hello everyone", "alice_h", days_ago=1),
        FakeMessage(5, "alice paid via venmo", "alice_h", days_ago=2, naive_ts=True),
    ]
    h = Harness(messages)
    h.install()
    deleted = await dues._delete_portal_messages_by_author(None, {"aliceh"})
    check("two of alice's posts deleted", 2, deleted)
    check("the recent and naive-stamped ones", [1, 5], sorted(h.deleted))
    check("bob's post kept", False, 2 in h.deleted)
    check("the 400-day-old post kept", False, 3 in h.deleted)
    check("the non-payment post kept", False, 4 in h.deleted)

    print("\n[3] no targets means no fetch at all")
    h = Harness(messages)
    h.install()
    check("returns zero", 0, await dues._delete_portal_messages_by_author(None, set()))
    check("portal never scanned", 0, h.fetch_calls)

    print("\n[4] a post matching two targets is deleted once")
    h = Harness([FakeMessage(9, "alice paid via venmo", "alice_h", days_ago=1)])
    h.install()
    deleted = await dues._delete_portal_messages_by_author(None, {"aliceh", "somethingelse"})
    check("deleted once", [9], h.deleted)
    check("counted once", 1, deleted)

    print("\n[5] logged message ids are deleted without scanning the portal")
    h = Harness([], log_ids=[101, 102])
    h.install()
    deleted = await dues._delete_logged_portal_messages(None, [("a@b.c", "Spring 2026")])
    check("both logged ids deleted", [101, 102], h.deleted)
    check("count returned", 2, deleted)
    check("portal not scanned", 0, h.fetch_calls)
    check("no emails means no work", 0, await dues._delete_logged_portal_messages(None, []))

    print("\n[6] a failing dues log does not stop the cleanup")
    h = Harness([FakeMessage(1, "alice paid via venmo", "alice_h", days_ago=1)])
    h.install()

    def boom(_pairs):
        raise RuntimeError("log unavailable")

    dues._dues_log_message_ids_for_emails = boom
    check("logged step reports zero", 0,
          await dues._delete_logged_portal_messages(None, [("a@b.c", None)]))
    deleted = await dues._cleanup_portal_messages_for_emails(
        None, [row("a@b.c", "alice_h")], [("a@b.c", "Spring 2026")]
    )
    check("author scan still ran", 1, deleted)
    check("the post was deleted", [1], h.deleted)

    print("\n[7] email-driven cleanup matches rows by email and semester")
    messages = [
        FakeMessage(1, "alice paid via venmo", "alice_h", days_ago=1),
        FakeMessage(2, "carol paid via venmo", "carol_h", days_ago=1),
    ]
    h = Harness(messages, log_ids=[])
    h.install()
    rows = [row("a@b.c", "alice_h", semester="Spring 2026"),
            row("c@b.c", "carol_h", semester="Fall 2025")]
    deleted = await dues._cleanup_portal_messages_for_emails(
        None, rows, [("a@b.c", "Spring 2026")]
    )
    check("only alice deleted", [1], h.deleted)
    check("count", 1, deleted)

    h = Harness(messages, log_ids=[])
    h.install()
    deleted = await dues._cleanup_portal_messages_for_emails(
        None, rows, [("c@b.c", "Spring 2026")]
    )
    check("a semester mismatch drops the row", [], h.deleted)
    check("count zero", 0, deleted)

    h = Harness(messages, log_ids=[])
    h.install()
    check("no emails means no work", 0,
          await dues._cleanup_portal_messages_for_emails(None, rows, []))

    print("\n[8] row-driven cleanup covers every handle a member listed")
    messages = [
        FakeMessage(1, "alice paid via venmo", "alice_old", days_ago=1),
        FakeMessage(2, "alice paid via venmo", "alice_new", days_ago=1),
        FakeMessage(3, "dave paid via venmo", "dave_h", days_ago=1),
    ]
    h = Harness(messages, log_ids=[])
    h.install()
    rows = [
        row("a@b.c", "alice_old / alice_new", semester="Spring 2026"),
        row("d@b.c", "dave_h", verified=False, semester="Spring 2026"),
    ]
    deleted = await dues._cleanup_portal_messages_for_verified_rows(None, rows, "Spring 2026")
    check("both of alice's handles matched", [1, 2], sorted(h.deleted))
    check("the unverified row is skipped", False, 3 in h.deleted)
    check("count", 2, deleted)

    h = Harness(messages, log_ids=[])
    h.install()
    check("a different semester matches nothing", 0,
          await dues._cleanup_portal_messages_for_verified_rows(None, rows, "Fall 2024"))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
