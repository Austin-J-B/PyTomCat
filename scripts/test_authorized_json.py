"""Write-endpoint gate regression: permissions, then CSRF, then the body.

Every endpoint that changes something — the feeding schedule, station
definitions, the fed/unfed checklist, sub claims — runs the same three checks
before it touches anything. They used to be copied into each handler, which is
how one of them ends up missing a CSRF check. _authorized_json is that sequence
in one place, and the order matters: a CSRF token is only meaningful once the
session is known good.

Run: python scripts/test_authorized_json.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from tomcat import main as tomcat_main

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


class Body:
    """Minimal stand-in for a request's payload stream.

    aiohttp's Request.read() drains its payload until it gets an empty chunk,
    so that is all this has to provide.
    """

    def __init__(self, raw: bytes):
        self._raw = raw

    async def readany(self) -> bytes:
        raw, self._raw = self._raw, b""
        return raw

    async def read(self, _n: int = -1) -> bytes:
        return await self.readany()


def request(body: str = '{"ok": true}'):
    return make_mocked_request("POST", "/api/thing", payload=Body(body.encode("utf-8")))


class Stubs:
    """Record how the gate was asked, and what each step answered."""

    def __init__(self, *, permission_error=None, csrf_error=None, session=None):
        self.calls: List[Dict[str, Any]] = []
        self._permission_error = permission_error
        self._csrf_error = csrf_error
        self._session = session if session is not None else {"user_id": "42"}

    def install(self) -> None:
        async def require_permissions(req, *, require_view=False, require_edit=False):
            self.calls.append({"step": "permissions", "view": require_view, "edit": require_edit})
            if self._permission_error is not None:
                return None, self._permission_error
            return self._session, None

        def require_csrf(req, session):
            self.calls.append({"step": "csrf", "session": session})
            return self._csrf_error

        tomcat_main._require_permissions = require_permissions
        tomcat_main._require_csrf = require_csrf

    @property
    def steps(self) -> List[str]:
        return [c["step"] for c in self.calls]


def run(stubs: Stubs, req=None, **kwargs):
    stubs.install()
    return asyncio.run(tomcat_main._authorized_json(req or request(), **kwargs))


def main() -> int:
    print("=" * 70)
    print("write endpoint gate regression tests")
    print("=" * 70)

    print("\n[1] the happy path returns the session and the parsed body")
    stubs = Stubs()
    session, data, error = run(stubs, require_view=True, require_edit=True)
    check("no error", None, error)
    check("session returned", {"user_id": "42"}, session)
    check("body parsed", {"ok": True}, data)
    check("both checks ran, in order", ["permissions", "csrf"], stubs.steps)

    print("\n[2] the permission flags are passed through as asked")
    for kwargs, want in [
        ({"require_view": True, "require_edit": True}, {"view": True, "edit": True}),
        ({"require_edit": True}, {"view": False, "edit": True}),
        ({"require_view": True}, {"view": True, "edit": False}),
    ]:
        stubs = Stubs()
        run(stubs, **kwargs)
        asked = {"view": stubs.calls[0]["view"], "edit": stubs.calls[0]["edit"]}
        check(f"{kwargs} -> {want}", want, asked)

    print("\n[3] a permission failure stops before CSRF and before the body")
    denied = web.Response(status=403, text="Forbidden")
    stubs = Stubs(permission_error=denied)
    session, data, error = run(stubs, require_edit=True)
    check("the permission response is returned", denied, error)
    check("no session leaked", None, session)
    check("no body returned", None, data)
    #Checking a CSRF token against a session that was never established is
    #meaningless, so the order here is the point.
    check("CSRF was never consulted", ["permissions"], stubs.steps)

    print("\n[4] a CSRF failure stops before the body")
    csrf_denied = web.Response(status=403, text="Bad CSRF token")
    stubs = Stubs(csrf_error=csrf_denied)
    session, data, error = run(stubs, require_edit=True)
    check("the CSRF response is returned", csrf_denied, error)
    check("no session leaked", None, session)
    check("no body returned", None, data)
    check("CSRF was checked against the session", {"user_id": "42"}, stubs.calls[1]["session"])

    print("\n[5] an unparseable body is a 400, not a traceback")
    for label, body in [
        ("not json", "this is not json"),
        ("empty body", ""),
        ("truncated json", '{"ok":'),
    ]:
        stubs = Stubs()
        session, data, error = run(stubs, request(body), require_edit=True)
        check(f"{label}: status", 400, error.status if error else None)
        check(f"{label}: message", "Invalid JSON", error.text if error else None)
        check(f"{label}: no body returned", None, data)

    print("\n[6] a body that parses to something other than an object comes through")
    #Handlers do their own shape checks; the gate only refuses what will not parse.
    stubs = Stubs()
    _session, data, error = run(stubs, request(json.dumps([1, 2, 3])), require_edit=True)
    check("a list is returned as-is", ([1, 2, 3], None), (data, error))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
