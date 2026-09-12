"""Tests for the event-loop blocking checker itself.

The checker is the only thing standing between a well-meant refactor and the
bot going connected-but-silent again, so it has to fire on the shapes that
caused that and stay quiet on the shapes that did not. A checker nobody trusts
gets an exemption added instead of a fix.

Run: python scripts/test_blocking_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_event_loop_blocking as checker

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def scan(source: str):
    """(number of direct hits, number of indirect hits) for one module."""
    direct, indirect, _reaches = checker.analyze({"m.py": source})
    return len(direct), len(indirect)


def main() -> int:
    print("=" * 70)
    print("event loop blocking checker tests")
    print("=" * 70)

    print("\n[1] a sheet call in an async body")
    check("flagged", (1, 0), scan('''
async def handler():
    return ws.get_all_values()
'''))
    check("in a thread, not flagged", (0, 0), scan('''
async def handler():
    return await asyncio.to_thread(ws.get_all_values)
'''))
    check("in a plain def, not flagged", (0, 0), scan('''
def helper():
    return ws.get_all_values()
'''))

    print("\n[2] a sync helper that reads the sheet, called from an async body")
    #This is the shape that hid the membership roster read behind the dues
    #corroboration check: nothing in the async function mentions gspread.
    check("flagged", (0, 1), scan('''
def load_rows():
    return ws.get_all_values()

async def handler():
    return load_rows()
'''))
    check("through two frames", (0, 1), scan('''
def load_rows():
    return ws.get_all_values()

def count_rows():
    return len(load_rows())

async def handler():
    return count_rows()
'''))
    check("the same helper in a thread", (0, 0), scan('''
def load_rows():
    return ws.get_all_values()

async def handler():
    return await asyncio.to_thread(load_rows)
'''))
    check("a helper that reads nothing", (0, 0), scan('''
def load_rows():
    return CACHE

async def handler():
    return load_rows()
'''))

    print("\n[3] work handed to a task or a thread is not this frame's work")
    #Every labeler warm endpoint is shaped like this: define the coroutine,
    #hand it to the loop, return. The sheet read happens after the handler has
    #already replied.
    check("create_task", (0, 0), scan('''
def schedule():
    async def _runner():
        await asyncio.to_thread(load_rows)
    asyncio.create_task(_runner())

def load_rows():
    return ws.get_all_values()

async def handler():
    schedule()
'''))
    check("an executor", (0, 0), scan('''
def load_rows():
    return ws.get_all_values()

async def handler():
    return loop.run_in_executor(None, load_rows)
'''))
    check("a pool submit", (0, 0), scan('''
def load_rows():
    return ws.get_all_values()

async def handler():
    return pool.submit(load_rows)
'''))

    print("\n[4] a barrier stops the chain")
    #_refresh_dyn_aliases is on the hot message path and reaches a sheet call,
    #but only from the background thread it starts.
    check("the barrier itself", (0, 0), scan('''
def _refresh_dyn_aliases():
    return ws.get_all_values()

async def handler():
    return _refresh_dyn_aliases()
'''))
    check("and its callers", (0, 0), scan('''
def _refresh_dyn_aliases():
    return ws.get_all_values()

def resolve_name(text):
    _refresh_dyn_aliases()
    return TABLE.get(text)

async def handler():
    return resolve_name("west")
'''))

    print("\n[5] a lambda body belongs to whoever runs it")
    check("not the enclosing async frame", (0, 0), scan('''
async def handler():
    return lambda: ws.get_all_values()
'''))

    print("\n[6] the real tree is clean")
    #Not a fresh scan -- just that main() agrees with itself.
    check("check returns success", 0, checker.main())

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
