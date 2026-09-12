"""Vision command regression: what detect, crop and identify post back.

The three commands share one runner in tomcat/handlers/vision.py — find the
image, acknowledge, download, hold the CV semaphore, watch for a cold start,
clean up — and differ only in the model call and how they render the result.
This test drives all three against a fake Discord channel and a stubbed model
so the shared scaffolding and each renderer stay pinned.

Run: python scripts/test_vision_handlers.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat.handlers import vision

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


class FakeAttachment:
    def __init__(self, url: str = "https://example.test/cat.jpg"):
        self.id = 1
        self.url = url
        self.filename = "cat.jpg"
        self.content_type = "image/jpeg"


class FakeMessage:
    def __init__(self, mid: int, channel, *, attachments=(), reference=None):
        self.id = mid
        self.channel = channel
        self.attachments = list(attachments)
        self.reference = reference
        self.author = type("A", (), {"id": 5, "name": "tester"})()
        self.guild = type("G", (), {"id": 9})()
        self.content = ""
        self.edits: List[dict] = []
        self.reactions: List[str] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self

    async def add_reaction(self, emoji):
        self.reactions.append(str(emoji))


class FakeChannel:
    def __init__(self):
        self.id = 77
        self.sent: List[Any] = []
        self.messages: List[FakeMessage] = []
        self._next_id = 1000

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self._next_id += 1
        msg = FakeMessage(self._next_id, self)
        self.messages.append(msg)
        return msg


class Out:
    def __init__(self, *, results=(), boxed=b"boxed", crops=None):
        self.results = list(results)
        self.boxed_jpeg = boxed
        if crops is not None:
            self.crops = crops


def install_stubs(out_or_exc, *, tmp_files: List[str]):
    """Neutralize the network/model/filesystem edges of the runner."""
    async def fake_notify():
        return None

    async def fake_download(att):
        path = f"/tmp/fake-{len(tmp_files)}.jpg"
        tmp_files.append(path)
        return path

    async def fake_read(path):
        return b"image-bytes"

    async def fake_cleanup(paths):
        for path in paths:
            if path in tmp_files:
                tmp_files.remove(path)

    async def fake_run_cv(fn, *args):
        if isinstance(out_or_exc, BaseException):
            raise out_or_exc
        return out_or_exc

    vision._notify_modal_activity_safe = fake_notify
    vision._download_attachment = fake_download
    vision._read_bytes = fake_read
    vision._cleanup = fake_cleanup
    vision._run_cv = fake_run_cv
    #Never arm the cold-start notice: it would leave a pending task behind.
    vision._maybe_cold_start_notice = lambda reply: None
    #register_identify_feedback writes to the feedback store; identify's own
    #renderer is what this test is about.
    vision.register_identify_feedback = lambda **kwargs: None
    vision._register_top5_listener = lambda *a: registered.append(a)


registered: List[Any] = []
logged: List[tuple] = []
vision.log_action = lambda *a: logged.append(a)


async def run_command(handler, out_or_exc, *, attachments=(1,), ctx_extra=None):
    tmp: List[str] = []
    install_stubs(out_or_exc, tmp_files=tmp)
    ch = FakeChannel()
    msg = FakeMessage(1, ch, attachments=[FakeAttachment()] if attachments else [])
    ctx: Dict[str, Any] = {"message": msg, "channel": ch}
    ctx.update(ctx_extra or {})
    await handler(None, ctx)
    reply = ch.messages[0] if ch.messages else None
    return ch, reply, tmp


async def main() -> int:
    print("=" * 70)
    print("vision command regression tests")
    print("=" * 70)

    print("\n[1] no image: each command names itself in the hint")
    for handler, want in [
        (vision.handle_cv_detect, "Attach an image or reply to one, then say `TomCat, detect`."),
        (vision.handle_cv_crop, "Attach an image or reply to one, then say `TomCat, crop`."),
        (vision.handle_cv_identify, "Attach an image or reply to one, then say `TomCat, identify`."),
    ]:
        ch, _reply, _tmp = await run_command(handler, Out(), attachments=())
        check(want.split("`")[1], want, ch.sent[0]["content"] if ch.sent else None)

    print("\n[2] silent_on_no_image suppresses the hint")
    ch, _reply, _tmp = await run_command(
        vision.handle_cv_detect, Out(), attachments=(), ctx_extra={"silent_on_no_image": True}
    )
    check("nothing sent", [], ch.sent)

    print("\n[3] detect reports the object count")
    for count, want in [(0, "Found 0 objects."), (1, "Found 1 object."), (3, "Found 3 objects.")]:
        ch, reply, tmp = await run_command(
            vision.handle_cv_detect, Out(results=[{"i": i} for i in range(count)])
        )
        check(f"{count} results", want, reply.edits[-1]["content"])
        check(f"{count} results: one attachment", 1, len(reply.edits[-1]["attachments"]))
        check(f"{count} results: temp file cleaned up", [], tmp)

    print("\n[4] crop batches attachments ten at a time")
    ch, reply, _tmp = await run_command(vision.handle_cv_crop, Out(crops=[b"a"]))
    check("one crop", "Cropped view:", reply.edits[-1]["content"])
    check("one crop: one attachment", 1, len(reply.edits[-1]["attachments"]))

    ch, reply, _tmp = await run_command(vision.handle_cv_crop, Out(crops=[b"a", b"b", b"c"]))
    check("three crops", "Cropped views (3 cats):", reply.edits[-1]["content"])
    check("three crops: three attachments", 3, len(reply.edits[-1]["attachments"]))

    ch, reply, _tmp = await run_command(
        vision.handle_cv_crop, Out(crops=[bytes([i]) for i in range(23)])
    )
    check("23 crops: first ten on the placeholder", 10, len(reply.edits[-1]["attachments"]))
    overflow = [s for s in ch.sent if "files" in s]
    check("23 crops: two overflow messages", 2, len(overflow))
    check("23 crops: overflow batch sizes", [10, 3], [len(s["files"]) for s in overflow])

    ch, reply, _tmp = await run_command(vision.handle_cv_crop, Out(crops=[]))
    check("no crops falls back to the boxed image", "Cropped view:", reply.edits[-1]["content"])

    print("\n[5] identify builds the ranked embed")
    registered.clear()
    results = [
        {"index": 1, "name": "Microwave", "conf": 0.912},
        {"index": 2, "name": "Twix", "conf": 0.4},
    ]
    ch, reply, tmp = await run_command(vision.handle_cv_identify, Out(results=results))
    embed = reply.edits[-1]["embed"]
    check("ranked lines",
          "1. **Microwave** (91.2%)\n2. **Twix** (40.0%)",
          embed.description)
    check("footer invites feedback", True, "React" in (embed.footer.text or ""))
    check("three reactions added", ["✅", "❌", "❓"], reply.reactions)
    check("top-5 listener registered", 1, len(registered))
    check("temp file cleaned up", [], tmp)

    registered.clear()
    ch, reply, _tmp = await run_command(vision.handle_cv_identify, Out(results=[]))
    check("no detection says so", "_no cat detected_", reply.edits[-1]["embed"].description)
    check("no detection: no reactions", [], reply.reactions)
    check("no detection: no listener", 0, len(registered))

    print("\n[6] failures land on the placeholder with the right wording")
    cases = [
        (vision.handle_cv_detect, asyncio.TimeoutError(),
         "Sorry, detection timed out. Try again in a moment.", "viz_detect_error"),
        (vision.handle_cv_crop, asyncio.TimeoutError(),
         "Sorry, crop timed out. Try again in a moment.", "viz_crop_error"),
        (vision.handle_cv_identify, asyncio.TimeoutError(),
         "Sorry, identify timed out. Try again in a moment.", "viz_identify_error"),
        (vision.handle_cv_detect, RuntimeError("boom"), "Sorry, detection failed.", "viz_detect_error"),
        (vision.handle_cv_crop, RuntimeError("boom"), "Sorry, crop failed.", "viz_crop_error"),
        (vision.handle_cv_identify, RuntimeError("boom"), "Sorry, identify failed.", "viz_identify_error"),
        (vision.handle_cv_detect, ValueError("that image is too small"),
         "that image is too small", None),
    ]
    for handler, exc, want_text, want_log in cases:
        logged.clear()
        ch, reply, tmp = await run_command(handler, exc)
        check(f"{type(exc).__name__} -> {want_text[:28]}...", want_text, reply.edits[-1]["content"])
        check("  placeholder cleared", ([], None),
              (reply.edits[-1]["attachments"], reply.edits[-1]["embed"]))
        check("  temp file cleaned up", [], tmp)
        if want_log:
            check(f"  logged as {want_log}", True, any(a[0] == want_log for a in logged))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
