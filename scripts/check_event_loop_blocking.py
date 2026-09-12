"""Fail the build on Google Sheets calls reachable from the event loop.

gspread is synchronous HTTP. A single get_all_values() is a network round trip,
and on the event loop it stalls every other handler along with the Discord
gateway heartbeat -- which is how the bot ends up connected but silent. There
were 27 such calls when this check was added, across the dues sheet writes, the
member profile commands, the finance ledger and the cat profile lookup.

The first pass flags a sheet method called straight from the body of an
`async def`. The second follows plain `def` helpers: an async frame that calls
a sync function which reaches a sheet call, however many frames down, is the
same stall wearing a hat. That pass found five more -- the membership roster
behind the dues corroboration check and the daily job, the ledger read-back in
the finance append retry loop, the station resident map, and the labeler's
manual-ref status endpoints.

A call is fine inside a plain `def` (something else decides how to run it), or
handed to something that runs it elsewhere -- asyncio.to_thread, create_task, an
executor. Functions in BARRIERS stop the second pass: they reach a sheet call
only down a path the event loop does not take.

Run: python scripts/check_event_loop_blocking.py
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["tomcat", "UserInterface"]

#Method names that belong to gspread and nothing else in this codebase, so a
#match is never a false positive on a dict or a local helper.
SHEET_CALLS = {
    "get_all_values", "get_all_records", "get_values", "values_get",
    "append_row", "append_rows", "insert_row", "insert_rows",
    "update_cell", "update_cells", "update_acell", "values_update",
    "batch_update", "batch_clear", "open_by_key", "open_by_url", "worksheet",
    "add_worksheet", "del_worksheet", "row_values", "col_values",
}

#Calls whose argument runs somewhere else -- another thread, or a task the
#loop picks up after this frame has returned. What is inside them is not part
#of the caller's own work, the same way a lambda body is not.
SPAWNERS = {
    "to_thread", "run_in_executor", "create_task", "ensure_future",
    "run_coroutine_threadsafe", "call_soon", "call_soon_threadsafe",
    "call_later", "submit", "apply_async", "add_done_callback",
}

#Reached from the event loop, but not down a blocking path.
BARRIERS = {
    #Kicks a background thread on TTL expiry and returns; callers keep serving
    #the in-memory table until it lands. Only force=True fetches inline, and
    #that one caller (handlers/admin.py) wraps it in asyncio.to_thread.
    "_refresh_dyn_aliases",
}


def _called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


class Visitor(ast.NodeVisitor):
    """Collect per-frame calls, plus the direct sheet calls in async frames."""

    def __init__(self, relative: str):
        self.relative = relative
        #(frame name, is async) for the function body we are inside.
        self.stack: List[Tuple[str, bool]] = []
        #Sheet calls sitting directly in an async body.
        self.direct_hits: List[Tuple[str, int, str, str]] = []
        #Sync frames that make a sheet call themselves: name -> sheet method.
        self.sheet_callers: Dict[str, str] = {}
        #Calls out of an async frame: (relative, line, frame, callee).
        self.async_calls: List[Tuple[str, int, str, str]] = []
        #Who calls whom, by name, so the taint can be propagated.
        self.edges: List[Tuple[str, str]] = []

    def _frame(self, node, is_async: bool) -> None:
        self.stack.append((node.name, is_async))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._frame(node, False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._frame(node, True)

    #A lambda body is called later, wherever the lambda ends up.
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.stack.append(("<lambda>", False))
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _called_name(node)
        if name in SPAWNERS or name in BARRIERS:
            #Whatever is in here runs off this frame by construction.
            return
        if not name or not self.stack:
            self.generic_visit(node)
            return
        frame, is_async = self.stack[-1]
        if name in SHEET_CALLS:
            if is_async:
                self.direct_hits.append((self.relative, node.lineno, frame, name))
            else:
                self.sheet_callers.setdefault(frame, name)
        elif is_async:
            self.async_calls.append((self.relative, node.lineno, frame, name))
        self.edges.append((frame, name))
        self.generic_visit(node)


def analyze(sources: Dict[str, str]) -> Tuple[
    List[Tuple[str, int, str, str]],
    List[Tuple[str, int, str, str, str]],
    Dict[str, str],
]:
    """Find the blocking calls in a set of {name: python source}.

    Returns (direct hits, indirect hits, sync functions that reach a sheet
    call). Separate from main() so the tests can hand it small files.
    """
    direct: List[Tuple[str, int, str, str]] = []
    sheet_callers: Dict[str, str] = {}
    async_calls: List[Tuple[str, int, str, str]] = []
    callers_of: Dict[str, Set[str]] = collections.defaultdict(set)

    for relative, source in sorted(sources.items()):
        visitor = Visitor(relative)
        visitor.visit(ast.parse(source))
        direct.extend(visitor.direct_hits)
        async_calls.extend(visitor.async_calls)
        for name, method in visitor.sheet_callers.items():
            sheet_callers.setdefault(name, method)
        for caller, callee in visitor.edges:
            callers_of[callee].add(caller)

    #Spread "reaches a sheet call" up through sync callers. Names, not
    #qualified paths: two helpers sharing a name are treated as one, which
    #can only make the check stricter.
    reaches: Dict[str, str] = dict(sheet_callers)
    frontier = list(reaches)
    while frontier:
        current = frontier.pop()
        for caller in callers_of.get(current, ()):
            if caller in reaches or caller in BARRIERS:
                continue
            reaches[caller] = reaches[current]
            frontier.append(caller)

    indirect = [
        (relative, lineno, frame, callee, reaches[callee])
        for relative, lineno, frame, callee in async_calls
        if callee in reaches
    ]
    return direct, indirect, reaches


def main() -> int:
    sources: Dict[str, str] = {}
    for target in TARGETS:
        for path in sorted((ROOT / target).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = str(path.relative_to(ROOT)).replace("\\", "/")
            #utf-8-sig: some files in this repo carry a byte-order mark, which
            #the import machinery tolerates and ast.parse does not.
            sources[relative] = path.read_text(encoding="utf-8-sig")

    try:
        direct, indirect, reaches = analyze(sources)
    except SyntaxError as exc:
        print(f"FAIL {exc.filename}: {exc}")
        return 1

    print("=" * 70)
    print("event loop blocking check")
    print("=" * 70)
    if direct or indirect:
        print()
        for relative, lineno, frame, name in sorted(direct):
            print(f"FAIL {relative}:{lineno}  {frame}() calls {name}() on the event loop")
        for relative, lineno, frame, callee, method in sorted(indirect):
            print(f"FAIL {relative}:{lineno}  {frame}() calls {callee}(), "
                  f"which reaches {method}() on the event loop")
        total = len(direct) + len(indirect)
        print(f"\n{total} blocking sheet call(s). Wrap them in asyncio.to_thread.")
        return 1
    print(f"\nNo Google Sheets calls on the event loop "
          f"({len(reaches)} sync function(s) that reach one, all called off it).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
