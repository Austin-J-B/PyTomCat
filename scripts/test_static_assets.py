"""Static asset serving regression: correct bytes, and not twice.

The labeler UI's files (index.html plus ~340KB of labeler.js and its helpers)
used to be read from disk and sent in full on every request. They are now cached
against the file's mtime and served with an ETag, so a browser that already has
the current version gets a 304 with no body — while still asking every time, so
a deploy is never missed.

Run: python scripts/test_static_assets.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import make_mocked_request

from tomcat import main as tomcat_main

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def request(path: str = "/asset.js", **headers):
    return make_mocked_request("GET", path, headers=headers)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="tomcat-assets-"))
    os.chdir(workdir)
    asset = workdir / "asset.js"

    def write(text: str) -> None:
        #newline="" so the CRLFs written here reach the file unchanged, matching
        #how labeler.js is actually stored.
        with asset.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    write("console.log('v1');\r\nconsole.log('two');\r\n")

    handler = tomcat_main._static_asset_route("asset.js", "application/javascript")
    missing = tomcat_main._static_asset_route(
        "gone.js", "application/javascript", missing_text="gone.js not found"
    )

    print("=" * 70)
    print("static asset serving regression tests")
    print("=" * 70)

    print("\n[1] a first request gets the file")
    resp = asyncio.run(handler(request()))
    check("status", 200, resp.status)
    #Read in text mode, so CRLF in the file goes out as LF, as it always has.
    check("newlines translated", b"console.log('v1');\nconsole.log('two');\n", resp.body)
    check("content type", "application/javascript; charset=utf-8",
          resp.headers["Content-Type"])
    check("revalidates every time", "no-cache", resp.headers["Cache-Control"])
    check("carries an ETag", True, resp.headers["ETag"].startswith('"'))
    etag = resp.headers["ETag"]

    print("\n[2] a client that already has it gets no body")
    resp = asyncio.run(handler(request(**{"If-None-Match": etag})))
    check("not modified", 304, resp.status)
    check("no body", b"", resp.body or b"")
    check("same ETag returned", etag, resp.headers["ETag"])

    print("\n[3] a stale or absent ETag gets the file")
    for label, headers in [
        ("no header", {}),
        ("different etag", {"If-None-Match": '"0000"'}),
        ("empty header", {"If-None-Match": ""}),
    ]:
        resp = asyncio.run(handler(request(**headers)))
        check(label, 200, resp.status)

    print("\n[4] the forms a browser actually sends")
    resp = asyncio.run(handler(request(**{"If-None-Match": f"W/{etag}"})))
    check("weak validator matches", 304, resp.status)
    resp = asyncio.run(handler(request(**{"If-None-Match": f'"0000", {etag}'})))
    check("one of several matches", 304, resp.status)
    resp = asyncio.run(handler(request(**{"If-None-Match": "*"})))
    check("a wildcard matches", 304, resp.status)

    print("\n[5] editing the file changes the ETag")
    #Same length, so the change has to be caught by mtime rather than size.
    os.utime(asset, (0, 0))
    write("console.log('v2');\r\nconsole.log('two');\r\n")
    resp = asyncio.run(handler(request(**{"If-None-Match": etag})))
    check("old ETag no longer matches", 200, resp.status)
    check("new bytes served", b"console.log('v2');\nconsole.log('two');\n", resp.body)
    new_etag = resp.headers["ETag"]
    check("ETag changed", True, new_etag != etag)
    resp = asyncio.run(handler(request(**{"If-None-Match": new_etag})))
    check("new ETag matches", 304, resp.status)

    print("\n[6] the file is read from disk only when it changes")
    import builtins
    reads = {"count": 0}
    original = builtins.open

    def counting_open(*args, **kwargs):
        if args and str(args[0]).endswith("asset.js"):
            reads["count"] += 1
        return original(*args, **kwargs)

    builtins.open = counting_open
    try:
        for _ in range(5):
            asyncio.run(handler(request()))
        check("no re-reads while unchanged", 0, reads["count"])
        os.utime(asset, (1, 1))
        asyncio.run(handler(request()))
        check("one read after a change", 1, reads["count"])
    finally:
        builtins.open = original

    print("\n[7] a missing file is a 404 with its own message")
    resp = asyncio.run(missing(request("/gone.js")))
    check("status", 404, resp.status)
    check("message", "gone.js not found", resp.text)

    print("\n[8] the prefix is part of what is served")
    handler_with_prefix = tomcat_main._static_asset_route(
        "asset.js", "application/javascript", prefix=b"/*flags*/\n"
    )
    resp = asyncio.run(handler_with_prefix(request()))
    check("prefix leads the body", True, resp.body.startswith(b"/*flags*/\n"))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
