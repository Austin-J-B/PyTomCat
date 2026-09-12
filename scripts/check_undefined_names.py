"""Fail the build on names that cannot resolve at runtime.

Most of this codebase's risky paths are error handlers and rarely-taken
branches, which is exactly where a typo'd name sits unnoticed. Four such bugs
were live at once when this check was added: a warm-up flag assigned into the
wrong scope so a detector pass ran on every poll, a comparison against a
variable that had been renamed, a log line referring to a name from another
function, and a message built from an `except ... as e` name after Python had
already unbound it.

Undefined names fail the run. The looser categories — unused locals and
redundant `global` declarations — are printed as notes, because one of them is
what led to the scope bug above, but plenty of them are harmless.

Run: python scripts/check_undefined_names.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["tomcat", "scripts", "UserInterface", "cloud"]

try:
    from pyflakes import api as pyflakes_api
    from pyflakes import reporter as pyflakes_reporter
    from pyflakes import messages as pyflakes_messages
except ImportError:
    print("pyflakes is not installed; skipping. (pip install pyflakes)")
    raise SystemExit(0)

#Anything here cannot resolve when the line runs.
FATAL = (
    pyflakes_messages.UndefinedName,
    pyflakes_messages.UndefinedLocal,
    pyflakes_messages.UndefinedExport,
)
#Worth reading, not worth blocking on.
NOTABLE = (
    pyflakes_messages.UnusedVariable,
    pyflakes_messages.UnusedIndirectAssignment,
) if hasattr(pyflakes_messages, "UnusedIndirectAssignment") else (
    pyflakes_messages.UnusedVariable,
)


class Collector(pyflakes_reporter.Reporter):
    """Sort pyflakes messages into the fatal ones and the rest."""

    def __init__(self) -> None:
        super().__init__(sys.stdout, sys.stderr)
        self.fatal: List[str] = []
        self.notable: List[str] = []
        self.errors: List[str] = []

    def flake(self, message) -> None:
        text = str(message)
        if isinstance(message, FATAL):
            self.fatal.append(text)
        elif isinstance(message, NOTABLE):
            self.notable.append(text)

    def unexpectedError(self, filename, msg) -> None:
        self.errors.append(f"{filename}: {msg}")

    def syntaxError(self, filename, msg, lineno, offset, text) -> None:
        self.errors.append(f"{filename}:{lineno}: syntax error: {msg}")


def main() -> int:
    collector = Collector()
    for target in TARGETS:
        path = ROOT / target
        if path.exists():
            pyflakes_api.checkRecursive([str(path)], collector)

    print("=" * 70)
    print("undefined name check")
    print("=" * 70)

    if collector.notable:
        print(f"\n{len(collector.notable)} note(s), not fatal:")
        for line in collector.notable:
            print(f"  {line}")

    if collector.errors:
        print(f"\n{len(collector.errors)} file(s) could not be parsed:")
        for line in collector.errors:
            print(f"  {line}")

    print()
    if collector.fatal or collector.errors:
        for line in collector.fatal:
            print(f"FAIL {line}")
        print(f"\n{len(collector.fatal) + len(collector.errors)} blocking problem(s).")
        return 1
    print("No undefined names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
