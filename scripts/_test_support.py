"""What a regression script needs in place before it imports the bot.

Import this first, above any `tomcat.*` import:

    import _test_support  # noqa: F401

Two things have to be true or the import itself raises, and neither has
anything to do with what the tests are checking:

  - The CV and Google stacks get pulled in by almost any module that reaches
    `tomcat.main`, and torch alone is a two-gigabyte download. CI installs
    requirements-test.txt and nothing else, so whatever is missing is stubbed.
    A test that actually exercises one of these has the real package installed
    and gets it; the stub only ever stands in for something absent.
  - `tomcat.main` refuses to import without UI_SESSION_SECRET, by design: the
    UI must never sign session cookies with a default. Tests are given a
    throwaway value, and none of them issues a cookie.

Both used to be copied into individual scripts, or -- worse -- left to a
developer's local .env and a cached CSV, so the tests only passed on one
machine. Keeping them here means there is one list to update.
"""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock

#Heavy or credential-bound packages that a bare checkout will not have.
OPTIONAL_DEPS = (
    "torch", "torch.nn", "torch.nn.functional", "torchvision",
    "torchvision.transforms", "ultralytics", "cv2", "numpy", "discord",
    "gspread", "gspread.auth", "gspread.exceptions", "gspread.utils",
    "google", "google.oauth2", "google.oauth2.service_account", "modal",
)


def stub_missing_optional_deps() -> list[str]:
    """Stand a MagicMock in for each optional package that will not import.

    Returns the names that were stubbed, so a test can say what it is missing.
    """
    stubbed: list[str] = []
    for name in OPTIONAL_DEPS:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = MagicMock()
            stubbed.append(name)
    return stubbed


def ensure_session_secret() -> None:
    """Give tomcat.main a session secret so importing it does not raise."""
    os.environ.setdefault("UI_SESSION_SECRET", "tests-do-not-sign-cookies")


def redirect_machine_log() -> str:
    """Send this process's machine log to a scratch directory.

    Six of the regression scripts exercise code that logs, and those records
    used to land in logs/machine beside the real ones -- 78 lines per suite
    run, including 63 finance events and mocked memory readings like
    rss=1000MB. That corpus is what production latency and behaviour get
    analysed from, so test output in it is not noise, it is wrong data.

    Must run before tomcat.logger is imported: it reads the directory once, at
    import, and keeps the day's file handle open.
    """
    existing = os.environ.get("TOMCAT_LOG_DIR")
    if existing:
        return existing
    path = tempfile.mkdtemp(prefix="tomcat-test-logs")
    os.environ["TOMCAT_LOG_DIR"] = path
    return path


STUBBED = stub_missing_optional_deps()
ensure_session_secret()
MACHINE_LOG_DIR = redirect_machine_log()
