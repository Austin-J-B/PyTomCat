"""Fail the build on blocking calls reachable from the event loop.

One blocking call stalls every other handler along with the Discord gateway
heartbeat -- which is how the bot ends up connected but silent.

What counts as blocking: a gspread method (synchronous HTTP, and a single
get_all_values() is a network round trip), time.sleep, a requests call, a
subprocess, or a torch.load. There were 27 sheet calls on the loop when this
check was added, across the dues sheet writes, the member profile commands,
the finance ledger and the cat profile lookup.

The first pass flags one of those called straight from the body of an
`async def`. The second follows plain `def` helpers: an async frame that calls
a sync function which blocks, however many frames down, is the same stall
wearing a hat. That pass found five sheet reads -- the membership roster behind
the dues corroboration check and the daily job, the ledger read-back in the
finance append retry loop, the station resident map, and the labeler's
manual-ref status endpoints -- and then, once torch.load was added to the list,
the gallery load that ran on the loop through the whole of startup.

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

#Other ways to stop the loop dead, matched on the dotted call target rather
#than a bare name: "run", "get" and "call" are far too common as method names
#to match on their own. A pattern matches the end of the chain, so
#"requests.get" catches an aliased import of the module too.
BLOCKING_PATTERNS = {
    #Blocks the thread for its whole duration. The async one is spelled
    #asyncio.sleep, so a match here is always a mistake.
    "time.sleep": "time.sleep()",
    #Synchronous HTTP. aiohttp is what the bot uses on the loop.
    "requests.get": "a blocking HTTP request",
    "requests.post": "a blocking HTTP request",
    "requests.put": "a blocking HTTP request",
    "requests.patch": "a blocking HTTP request",
    "requests.delete": "a blocking HTTP request",
    "requests.head": "a blocking HTTP request",
    "requests.request": "a blocking HTTP request",
    "request.urlopen": "a blocking HTTP request",
    #Waits on another process.
    "subprocess.run": "a subprocess",
    "subprocess.call": "a subprocess",
    "subprocess.check_call": "a subprocess",
    "subprocess.check_output": "a subprocess",
    #The encoder is 1.2GB and the gallery 25MB; both take seconds off disk.
    "torch.load": "a torch.load()",
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
    """The bare name being called, e.g. "get_all_values"."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _dotted_name(node: ast.Call) -> str:
    """The call target as written, e.g. "time.sleep" or "self.ws.append_row"."""
    parts: List[str] = []
    current = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _blocking_label(node: ast.Call) -> str:
    """What this call blocks on, or "" if it is not one we know about."""
    name = _called_name(node)
    if name in SHEET_CALLS:
        return f"{name}()"
    dotted = _dotted_name(node)
    for pattern, label in BLOCKING_PATTERNS.items():
        if dotted == pattern or dotted.endswith("." + pattern):
            return label
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
        label = _blocking_label(node)
        if label:
            if is_async:
                self.direct_hits.append((self.relative, node.lineno, frame, label))
            else:
                self.sheet_callers.setdefault(frame, label)
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
        for relative, lineno, frame, label in sorted(direct):
            print(f"FAIL {relative}:{lineno}  {frame}() calls {label} on the event loop")
        for relative, lineno, frame, callee, label in sorted(indirect):
            print(f"FAIL {relative}:{lineno}  {frame}() calls {callee}(), "
                  f"which reaches {label} on the event loop")
        total = len(direct) + len(indirect)
        print(f"\n{total} blocking call(s) on the event loop. "
              f"Wrap them in asyncio.to_thread.")
        return 1
    print(f"\nNothing blocking on the event loop "
          f"({len(reaches)} sync function(s) that block, all called off it).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
