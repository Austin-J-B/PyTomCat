"""Fail the build on Google Sheets calls made from the event loop.

gspread is synchronous HTTP. A single get_all_values() is a network round trip,
and on the event loop it stalls every other handler along with the Discord
gateway heartbeat — which is how the bot ends up connected but silent. There
were 27 such calls when this check was added, across the dues sheet writes, the
member profile commands, the finance ledger and the cat profile lookup.

A call is fine if it happens inside a plain `def` (something else decides how to
run it) or inside asyncio.to_thread(...). It is flagged only when it sits
directly in the body of an `async def`.

Run: python scripts/check_event_loop_blocking.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import List, Tuple

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


class Visitor(ast.NodeVisitor):
    def __init__(self, relative: str):
        self.relative = relative
        self.stack: List[Tuple[str, str]] = []
        self.hits: List[Tuple[str, int, str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(("sync", node.name))
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(("async", node.name))
        self.generic_visit(node)
        self.stack.pop()

    #A lambda body is called later, wherever the lambda ends up.
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.stack.append(("sync", "<lambda>"))
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = self._called_name(node)
        if name == "to_thread":
            #Whatever is in here runs off the loop by construction.
            return
        if name in SHEET_CALLS and self.stack and self.stack[-1][0] == "async":
            self.hits.append((self.relative, node.lineno, self.stack[-1][1], name))
        self.generic_visit(node)

    @staticmethod
    def _called_name(node: ast.Call) -> str:
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
        return ""


def main() -> int:
    hits: List[Tuple[str, int, str, str]] = []
    for target in TARGETS:
        for path in sorted((ROOT / target).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                #utf-8-sig: some files in this repo carry a byte-order mark,
                #which the import machinery tolerates and ast.parse does not.
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError as exc:
                print(f"FAIL {path}: {exc}")
                return 1
            relative = str(path.relative_to(ROOT)).replace("\\", "/")
            visitor = Visitor(relative)
            visitor.visit(tree)
            hits.extend(visitor.hits)

    print("=" * 70)
    print("event loop blocking check")
    print("=" * 70)
    if hits:
        print()
        for relative, lineno, func, name in sorted(hits):
            print(f"FAIL {relative}:{lineno}  {func}() calls {name}() on the event loop")
        print(f"\n{len(hits)} blocking sheet call(s). Wrap them in asyncio.to_thread.")
        return 1
    print("\nNo Google Sheets calls on the event loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
