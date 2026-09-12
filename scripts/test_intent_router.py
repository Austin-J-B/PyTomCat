"""Intent routing regression: messages in, intents out.

The router turns Discord messages into intents through an ordered set of rules,
and the order is load-bearing — "recache catabase" has to be checked before the
photo-cache commands, "who is feeding today" before "who is feeding". This test
drives IntentRouter with fake messages and pins the verdict for one message per
intent type, plus the officer gate and the cases that must stay silent.

Run: python scripts/test_intent_router.py
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#Stubs whatever of the CV and Google stacks is not installed, and gives
#tomcat.main a session secret. Must come before any tomcat import.
import _test_support  # noqa: F401

import tomcat.aliases as _aliases

#The cats these cases name. Without the CatDatabase -- no credentials in CI,
#and the local CSV cache is not in the repo -- nothing resolved and the tests
#only passed on a machine that happened to have the cache. They bring their
#own list instead, so what resolves is the same everywhere.
_CATS = ["Microwave", "Eraser", "Twix", "Eggs", "Pencil", "Snickers", "Ford F-150"]
_FIXTURE_CSV = Path(tempfile.mkdtemp(prefix="aliases")) / "CatDatabase.csv"
_FIXTURE_CSV.write_text(
    "\n".join(["Full Name,Common Nicknames"] + [f"{name}," for name in _CATS]) + "\n",
    encoding="utf-8",
)
_aliases._FALLBACK_CSV_PATHS = [_FIXTURE_CSV]

#Resolve cat aliases once up front: the background refresh would otherwise swap
#the table mid-run and change which names resolve.
_aliases._do_dyn_alias_refresh()

from tomcat import intent_router as router_mod
from tomcat.config import settings

FEED_CH = 90001
OTHER_CH = 90002

TODAY = date.today().isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


class FakeAttachment:
    def __init__(self, aid: int, content_type: str = "image/png"):
        self.id = aid
        self.content_type = content_type


class FakeAuthor:
    def __init__(self, uid: int = 777):
        self.id = uid
        self.name = "tester"
        self.roles: List[Any] = []


class FakeChannel:
    def __init__(self, cid: int, name: str):
        self.id = cid
        self.name = name


class FakeMessage:
    def __init__(self, mid: int, content: str, channel: FakeChannel, *, image: bool):
        self.id = mid
        self.content = content
        self.channel = channel
        self.author = FakeAuthor()
        self.attachments = [FakeAttachment(4242)] if image else []
        self.mentions: List[Any] = []
        self.reference = None


#(officer, channel, has_image, text, expected intent, expected slots)
CASES = [
    #--- officer-gated commands: granted ---
    (True, FEED_CH, False, "tomcat check the last email", "gmail_check_last", {}),
    (True, FEED_CH, False, "tomcat log the past 12 emails", "gmail_log_recent", {}),
    (True, FEED_CH, False, "tomcat check emails", "gmail_check_emails", {}),
    (True, FEED_CH, False, "tomcat scan emails", "gmail_check_emails", {}),
    (True, FEED_CH, False, "tomcat log last 20 finances", "finance_log_recent", {}),
    (True, FEED_CH, False, "tomcat auth code https://example.test/x?code=abc", "gmail_auth_code", {}),
    (True, FEED_CH, False, "tomcat check dues", "dues_check", {}),
    (True, FEED_CH, False, "tomcat check dues payments", "dues_check", {}),
    (True, FEED_CH, False, "tomcat run the dues perks", "dues_perks", {}),
    (True, FEED_CH, False, "tomcat update dues paying members", "dues_update", {}),
    (True, FEED_CH, False, "tomcat run dues job", "dues_run_job", {}),
    (True, FEED_CH, False, "tomcat remove role 123456789 from everyone", "role_remove_all", {}),
    (True, FEED_CH, False, "tomcat strip the role 999888777 from all", "role_remove_all", {}),
    (True, FEED_CH, False, "tomcat timeout <@99> for 5 minutes", "timeout_user", {"target_user_id": 99}),
    (True, FEED_CH, False, "tomcat recache catabase", "recache_catabase", {}),
    (True, FEED_CH, False, "tomcat recache names", "recache_catabase", {}),
    #--- officer-gated commands: refused, silently ---
    (False, FEED_CH, False, "tomcat check the last email", "none", {}),
    (False, FEED_CH, False, "tomcat read emails", "none", {}),
    (False, FEED_CH, False, "tomcat check dues", "none", {}),
    (False, FEED_CH, False, "tomcat recache catabase", "none", {}),
    (False, FEED_CH, False, "tomcat timeout <@99> for 5 minutes", "none", {}),
    #--- open commands ---
    (False, FEED_CH, False, "tomcat silent mode on", "silent_mode", {}),
    (False, FEED_CH, False, "tomcat feeding schedule link", "feeding_schedule_link", {}),
    (False, FEED_CH, False, "tomcat what is the feeding schedule", "feeding_schedule_link", {}),
    (False, FEED_CH, False, "tomcat sub request link", "sub_request_link", {}),
    (False, FEED_CH, False, "tomcat cover my shift", "sub_request_link", {}),
    (False, FEED_CH, False, "tomcat functions", "function_glossary", {}),
    (False, FEED_CH, False, "tomcat help", "function_glossary", {}),
    (False, FEED_CH, False, "tomcat feeding update", "feeding_status", {}),
    (False, FEED_CH, False, "tomcat who has been fed today?", "feeding_status", {}),
    (False, FEED_CH, False, "tomcat who is feeding today", "feeding_today", {}),
    (False, FEED_CH, False, "tomcat who is feeding tomorrow", "feeding_schedule", {"dates": [TOMORROW]}),
    (False, FEED_CH, False, "tomcat manual 8pm update", "manual_8pm", {}),
    (False, FEED_CH, False, "tomcat create profiles 3 through 9", "profiles_create", {}),
    (False, FEED_CH, False, "tomcat update profile 7", "profile_update_one", {}),
    (False, FEED_CH, False, "tomcat update all profiles", "profiles_update_all", {}),
    #--- cat lookups ---
    (False, FEED_CH, False, "tomcat show me microwave", "show_photo", {"cat_name": "Microwave"}),
    (False, FEED_CH, False, "tomcat photo of eraser", "show_photo", {"cat_name": "Eraser"}),
    (False, FEED_CH, False, "tomcat who is microwave", "who_is", {"cat_name": "Microwave"}),
    (False, FEED_CH, False, "tomcat whos eraser", "who_is", {"cat_name": "Eraser"}),
    (False, FEED_CH, False, "tomcat show me", "none", {}),
    #--- vision, with an attachment present ---
    (False, FEED_CH, True, "tomcat identify this", "cv_identify", {}),
    (False, FEED_CH, True, "tomcat who is this?", "cv_identify", {}),
    (False, FEED_CH, True, "tomcat detect", "cv_detect", {}),
    (False, FEED_CH, True, "tomcat crop", "cv_crop", {}),
    #--- feeding updates ---
    (False, FEED_CH, False, "fed west hall", "feed_update", {"station": "West Hall", "dates": [TODAY]}),
    (False, FEED_CH, False, "i fed west hall today", "feed_update", {"station": "West Hall", "dates": [TODAY]}),
    (False, FEED_CH, False, "fed microwave", "feed_update", {"station": "Microwave", "dates": [TODAY]}),
    (False, FEED_CH, False, "i didn't feed west hall", "none", {}),
    (False, FEED_CH, False, "did anyone feed west hall?", "none", {}),
    #--- noise stays quiet ---
    (False, FEED_CH, False, "", "none", {}),
    (False, FEED_CH, False, "hello", "none", {}),
    (False, FEED_CH, False, "lol", "none", {}),
    (False, FEED_CH, False, "thanks!", "none", {}),
    (False, OTHER_CH, False, "hello everyone how is it going", "none", {}),
]

#Cat-database questions: only the parsed operation and filters are pinned.
QUERY_CASES = [
    ("tomcat what cats are orange", "list_names_by_filters", {"color_family": "orange"}),
    ("tomcat how many cats are there", "count_all_cats", {}),
    ("tomcat which cats live at west hall", "list_names_by_filters", {"location": "West Hall"}),
]

FAILURES: List[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL {msg}")


async def verdict(officer: bool, channel_id: int, image: bool, text: str, mid: int):
    router_mod.is_officer = lambda *_a, **_k: officer  # type: ignore[assignment]
    router = router_mod.IntentRouter()
    msg = FakeMessage(mid, text, FakeChannel(channel_id, "chan"), image=image)
    row = router._machine_row_from_message(msg)
    router._buf[(row["channel_id"], row["user_id"])].append(row)
    return await router._analyze_with_context(row, msg)


async def main() -> int:
    settings.ch_feeding_team = FEED_CH
    settings.allowed_feeding_channel_ids = [FEED_CH]
    mid = itertools.count(9_100_000)

    print("=" * 70)
    print("intent router regression tests")
    print("=" * 70)

    print("\n[1] one message per intent type")
    for officer, channel_id, image, text, want_type, want_slots in CASES:
        ev = await verdict(officer, channel_id, image, text, next(mid))
        label = f"{text!r} (officer={officer}, image={image})"
        if ev is None:
            fail(f"{label}: router returned None")
            continue
        if ev.type != want_type:
            fail(f"{label}: expected {want_type!r}, got {ev.type!r}")
            continue
        bad = {k: getattr(ev, k) for k, v in want_slots.items() if getattr(ev, k) != v}
        if bad:
            fail(f"{label}: slots {bad!r} != {want_slots!r}")
            continue
        print(f"  PASS {label} -> {ev.type}")

    print("\n[2] cat-database questions parse to an operation")
    for text, want_op, want_filters in QUERY_CASES:
        ev = await verdict(False, FEED_CH, False, text, next(mid))
        label = f"{text!r}"
        query: Optional[Dict[str, Any]] = getattr(ev, "query", None) if ev else None
        if not ev or ev.type != "cat_query" or not query:
            fail(f"{label}: expected a cat_query with a parsed query, got {ev and ev.type!r}")
            continue
        if query.get("op") != want_op:
            fail(f"{label}: op {query.get('op')!r} != {want_op!r}")
            continue
        bad = {k: query.get(k) for k, v in want_filters.items() if query.get(k) != v}
        if bad:
            fail(f"{label}: filters {bad!r} != {want_filters!r}")
            continue
        print(f"  PASS {label} -> {want_op}")

    print("\n[3] a refused officer command records why it stayed silent")
    router_mod.is_officer = lambda *_a, **_k: False  # type: ignore[assignment]
    router = router_mod.IntentRouter()
    msg = FakeMessage(next(mid), "tomcat check dues", FakeChannel(FEED_CH, "chan"), image=False)
    row = router._machine_row_from_message(msg)
    ev = await router._analyze_with_context(row, msg)
    trace = router._traces.get(row["message_id"], [])
    if not ev or ev.type != "none":
        fail(f"expected a silent verdict, got {ev and ev.type!r}")
    elif "deny:not_officer" not in trace:
        fail(f"expected deny:not_officer in the trace, got {trace!r}")
    else:
        print("  PASS denial recorded as deny:not_officer")

    print("\n[4] the trace map stays bounded")
    router = router_mod.IntentRouter()
    for i in range(2000):
        router._traces[i] = ["step"]
    if len(router._traces) > 1024:
        fail(f"trace map grew to {len(router._traces)} entries")
    else:
        print(f"  PASS {len(router._traces)} entries after 2000 writes")

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
