"""Regression test for "show me <cat>" name lookup.

Covers two bugs:
  * get_cat_profile stripped every digit from the sheet name, so "3. Ford F-150"
    never matched "Ford F-150".
  * "show me" read the whole CatDatabase sheet on every request; it now uses an
    exact match from the hourly profile cache and only reads live on a miss.

Runs fully offline: the sheet client and live lookup are stubbed.

Run:  python scripts/test_cat_lookup.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tomcat.services.catsheets as catsheets  # noqa: E402
from tomcat.services import profile_cache as PC  # noqa: E402
import tomcat.handlers.cats as cats  # noqa: E402

FAILURES: list[str] = []

SHEET = [
    ["Full Name"],
    ["1. Microwave"],
    ["3. Ford F-150"],
    ["14. Pencil 2"],
    ["56. Airbus A320 Neo"],
    ["57. Eden"],
    ["80. Ed Sheeran"],
]


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


def _actual(result) -> str:
    return result["actual_name"] if isinstance(result, dict) else str(result)


def test_live_lookup_keeps_digits_in_names():
    print("\n[1] live sheet lookup keeps digits inside names")
    ws = SimpleNamespace(get_all_values=lambda: SHEET)
    book = SimpleNamespace(worksheet=lambda _name: ws)
    catsheets.sheets_client = lambda: SimpleNamespace(open_by_key=lambda _sid: book)
    catsheets.settings = SimpleNamespace(sheet_catabase_id="test-sheet")

    for query, want in [
        ("Ford F-150", "3. Ford F-150"),
        ("ford f150", "3. Ford F-150"),
        ("Pencil 2", "14. Pencil 2"),
        ("Airbus A320 Neo", "56. Airbus A320 Neo"),
        ("Microwave", "1. Microwave"),
    ]:
        got = _actual(asyncio.run(catsheets.get_cat_profile(query)))
        check("%r -> %s" % (query, want), got == want, got)

    got = asyncio.run(catsheets.get_cat_profile("Ford F-"))
    check("digit-stripped name no longer matches", isinstance(got, str), _actual(got))


def test_show_me_uses_cache_first():
    print("\n[2] show-me lookup: exact cache hit, live read only on miss")
    PC._CACHE = {
        PC._norm(PC._display_from_full(row[0])): {"actual_name": row[0]}
        for row in SHEET[1:]
    }
    live_calls: list[str] = []

    async def fake_live(name: str):
        live_calls.append(name)
        return "No match for '%s'." % name

    cats.get_cat_profile = fake_live

    for query, want in [
        ("Ford F-150", "3. Ford F-150"),
        ("ford f150", "3. Ford F-150"),
        ("3. Ford F-150", "3. Ford F-150"),
        ("Pencil 2", "14. Pencil 2"),
    ]:
        got = _actual(asyncio.run(cats._lookup_cat_profile(query)))
        check("%r -> %s (cached)" % (query, want), got == want, got)
    check("cache hits never read the sheet", live_calls == [], repr(live_calls))

    got = asyncio.run(cats._lookup_cat_profile("Ed"))
    check("partial name does not substring-match Eden/Ed Sheeran", isinstance(got, str), _actual(got))
    asyncio.run(cats._lookup_cat_profile("Brand New Cat"))
    check("cache misses fall back to live read", live_calls == ["Ed", "Brand New Cat"], repr(live_calls))


def test_who_is_uses_cache_first():
    print("\n[3] who-is profile card: same exact cache lookup")
    live_calls: list[str] = []

    async def fake_live(name: str):
        live_calls.append(name)
        return "No match for '%s'." % name

    async def no_image(_actual: str):
        return None, None

    cats.get_cat_profile = fake_live
    cats._build_latest_profile_image_payload = no_image

    def run(name: str):
        sent: list[dict] = []

        class Channel:
            async def send(self, content=None, **kwargs):
                sent.append({"content": content, **kwargs})

        intent = SimpleNamespace(data={"name": name})
        asyncio.run(cats.handle_cat_profile(intent, {"channel": Channel()}))
        return sent

    sent = run("ford f150")
    embed = sent[0].get("embed") if sent else None
    title = getattr(embed, "title", "") or ""
    check("who is ford f150 -> Ford F-150 card", "Ford F-150" in title, repr(sent))
    check("cache hit never reads the sheet", live_calls == [], repr(live_calls))

    sent = run("Ed")
    check("who is Ed does not substring-match", bool(sent) and "No match" in str(sent[0].get("content")), repr(sent))
    check("miss falls back to live read", live_calls == ["Ed"], repr(live_calls))


def main():
    print("=" * 70)
    print("cat lookup regression tests")
    print("=" * 70)
    test_live_lookup_keeps_digits_in_names()
    test_show_me_uses_cache_first()
    test_who_is_uses_cache_first()
    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
