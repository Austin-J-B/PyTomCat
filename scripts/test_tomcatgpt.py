"""TomCatGPT routing: which messages go to the language model, and what thread it sees.

Pins the call tag's spellings (and what must NOT count: "TomCat," commands, "tomorrow", "Tom said"), the
reply-thread walk (a reply to a TomCatGPT answer continues the thread; a reply to an ordinary command answer
does not), text cleaning, and the worker protocol against a fake worker. No torch and no model: the
model side is checked locally against the dashboard (export_tomcatgpt_bundle.py + the parity check).

Run: python scripts/test_tomcatgpt.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _test_support  # noqa: F401,E402

import tomcat.handlers.tomcatgpt as gpt  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


TAGGED = {
    "TomCatGPT, what's up?": "what's up?",
    "tomcatgpt what's up": "what's up",
    "TomCat GPT: who is microwave": "who is microwave",
    "tom cat gpt - hi": "hi",
    "TomCat-GPT, hi": "hi",
    "Tomcat_gpt hi": "hi",
    "TOMCATGPT!!! hi": "hi",
    "tomkatgpt, hi": "hi",
    "TomGPT, hi": "hi",
    "tomcat gtp hi": "hi",
    "TomCatGPT4, hi": "hi",
    "@TomCatGPT hi": "hi",
    "hey tomcatgpt, how are you": "how are you",
    "  TomCatGPT,\nmulti\nline": "multi\nline",
    "TomCatGPT": "",
    "TomCatGPT?": "",
    "tomactgpt, hi": "hi",           # typo
    "tocmatgpt hi": "hi",            # typo
    "TomCatGPT, show me microwave": "show me microwave",
    "TomCatGPT, meow": "meow",
}
UNTAGGED = [
    "TomCat, show me microwave",
    "tomcat who is eraser",
    "Tom, show me sn123",
    "tomorrow's feeding",
    "Tom said gpt is bad",
    "gpt tomcat",
    "I asked TomCatGPT something",
    "tomcatgptx hi",
    "chatgpt, hi",
    "",
]
for text, want in TAGGED.items():
    check(f"tag {text!r}", gpt.strip_call_tag(text), want)
for text in UNTAGGED:
    check(f"no tag {text!r}", gpt.strip_call_tag(text), None)

check("clean mention", gpt.clean_text("hi <@123> in <#456> <:wheeze:789> https://x.y/z"),
      "hi @user in #channel :wheeze: <url>")
check("clean mash", gpt.clean_text("a" * 40), "a" * 12)

BOT, ALICE, BOB = 1, 10, 20


class Channel:
    def __init__(self):
        self.id = 99
        self.messages: dict[int, "Message"] = {}

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise LookupError(mid)
        return self.messages[mid]

    def typing(self):
        class _T:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False
        return _T()


CH = Channel()
_ids = iter(range(1000, 100000))


class Message:
    def __init__(self, author, content, reply_to=None, image=False):
        self.id = next(_ids)
        self.author = SimpleNamespace(id=author, bot=author == BOT)
        self.content = content
        self.channel = CH
        self.attachments = [SimpleNamespace(content_type="image/png")] if image else []
        # resolved left empty: the walk must fetch, as it does for older messages
        self.reference = (SimpleNamespace(message_id=reply_to.id, channel_id=CH.id, resolved=None, cached_message=None)
                          if reply_to else None)
        self.replies: list[str] = []
        CH.messages[self.id] = self

    async def reply(self, text, **kwargs):
        assert kwargs.get("allowed_mentions") is not None, "TomCatGPT must send with allowed_mentions"
        self.replies.append(text)
        return Message(BOT, text, reply_to=self)


def thread(msg):
    return asyncio.run(gpt.build_thread(msg, BOT))


# A fresh question.
q1 = Message(ALICE, "TomCatGPT, who is the fattest cat?")
check("fresh", thread(q1), [{"s": 1, "text": "who is the fattest cat?"}])
# Continue by replying to the answer, without the tag.
a1 = Message(BOT, "Microwave, easily", reply_to=q1)
q2 = Message(ALICE, "is he friendly?", reply_to=a1)
check("continue", thread(q2), [{"s": 1, "text": "who is the fattest cat?"}, {"s": 2, "text": "Microwave, easily"},
                              {"s": 1, "text": "is he friendly?"}])
# Someone else joins the thread with a picture: a new speaker number (3), image flag.
a2 = Message(BOT, "yes", reply_to=q2)
q3 = Message(BOB, "what about this one", reply_to=a2, image=True)
check("third speaker", [(m["s"], m.get("img", False)) for m in thread(q3)],
      [(1, False), (2, False), (1, False), (2, False), (3, True)])
# Retyping the tag mid-thread keeps the context.
q3b = Message(ALICE, "TomCatGPT, and Eraser?", reply_to=a2)
check("tag mid-thread", [m["text"] for m in thread(q3b)][-1], "and Eraser?")
check("tag mid-thread length", len(thread(q3b)), 5)
# A reply to an ordinary command answer stays with the router.
c1 = Message(ALICE, "TomCat, show me microwave")
c1a = Message(BOT, "[photo]", reply_to=c1)
c2 = Message(ALICE, "so cute", reply_to=c1a)
check("command reply", thread(c2), None)
# ... unless it is tagged: then it is a fresh TomCatGPT question.
c3 = Message(ALICE, "TomCatGPT, is he cute?", reply_to=c1a)
check("tagged command reply", thread(c3), [{"s": 1, "text": "is he cute?"}])
# A tagged reply to a friend's message starts fresh too.
f1 = Message(BOB, "lunch?")
f2 = Message(ALICE, "TomCatGPT what should we eat", reply_to=f1)
check("tagged reply to person", thread(f2), [{"s": 1, "text": "what should we eat"}])
# Plain messages and replies to people are not for TomCatGPT.
check("plain", thread(Message(ALICE, "hello")), None)
check("reply to person", thread(Message(ALICE, "same", reply_to=f1)), None)
# A deleted message in the middle cuts the thread there: the part after it has no tagged root.
d1 = Message(ALICE, "TomCatGPT hi")
d1a = Message(BOT, "hey", reply_to=d1)
d2 = Message(ALICE, "what's new", reply_to=d1a)
d2a = Message(BOT, "not much", reply_to=d2)
del CH.messages[d2.id]
check("deleted middle", thread(Message(ALICE, "cool", reply_to=d2a)), None)
# Forwarding one of TomCatGPT's answers is not a reply: no thread, no hint.
fw = Message(ALICE, "", reply_to=a1)
fw.reference.type = SimpleNamespace(name="forward")
check("forward of answer", thread(fw), None)
# Empty question: an empty list (the handler answers with a hint).
check("empty", thread(Message(ALICE, "TomCatGPT")), [])
# Long threads are capped.
node = Message(ALICE, "TomCatGPT start")
for i in range(30):
    node = Message(BOT, f"bot {i}", reply_to=node)
    node = Message(ALICE, f"me {i}", reply_to=node)
long = thread(node)
check("cap", len(long) <= gpt.MAX_THREAD and long[-1]["text"] == "me 29" and long[0]["s"] == 1, True)


# The worker protocol, against a fake worker script (no torch).
FAKE = textwrap.dedent("""
    import json, sys
    print(json.dumps({"ready": True, "load_seconds": 0, "step": 1}), flush=True)
    for line in sys.stdin:
        r = json.loads(line)
        if r["messages"][-1]["text"] == "die":
            sys.exit(3)
        if r["messages"][-1]["text"] == "oom":  # what the kernel's OOM killer does
            import os, signal
            os.kill(os.getpid(), signal.SIGKILL)
        print(json.dumps({"id": r["id"], "text": "echo " + r["messages"][-1]["text"], "candidates": 4,
                          "seconds": 0}), flush=True)
""")


async def worker_checks():
    tmp = Path(tempfile.mkdtemp(prefix="tomcatgpt"))
    (tmp / "manifest.json").write_text("{}", encoding="utf-8")
    script = tmp / "fake_worker.py"
    script.write_text(FAKE, encoding="utf-8")
    gpt.settings.tomcatgpt_dir = str(tmp)
    worker = gpt.Worker()
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        return await real_exec(sys.executable, str(script), **kwargs)
    gpt.asyncio.create_subprocess_exec = fake_exec
    try:
        out = await worker.reply([{"s": 1, "text": "hello"}])
        check("worker reply", out.get("text"), "echo hello")
        pid = worker.proc.pid
        out = await worker.reply([{"s": 1, "text": "again"}])
        check("worker stays loaded", (out.get("text"), worker.proc.pid), ("echo again", pid))
        try:  # the worker dies mid-request: one restart, then it fails again on the same input
            await worker.reply([{"s": 1, "text": "die"}])
            FAILURES.append("dead worker: no error raised")
        except RuntimeError:
            pass
        out = await worker.reply([{"s": 1, "text": "back"}])
        check("worker restarts", out.get("text"), "echo back")

        # Killed by a signal (the OOM killer): no retry, a WorkerOOM the handler turns into the OOM message.
        if hasattr(gpt.signal, "SIGKILL"):  # POSIX: CI and the server
            try:
                await worker.reply([{"s": 1, "text": "oom"}])
                FAILURES.append("oom: no WorkerOOM raised")
            except gpt.WorkerOOM:
                pass
            gpt.WORKER = worker
            m = Message(ALICE, "TomCatGPT oom")
            await gpt.maybe_handle(m, SimpleNamespace(user=SimpleNamespace(id=BOT)))
            check("oom message", m.replies, [gpt.OOM_MESSAGE])
            out = await worker.reply([{"s": 1, "text": "after oom"}])
            check("recovers after oom", out.get("text"), "echo after oom")

        # Idle: the model is unloaded a while after the last question and loads again on the next one.
        worker.idle_seconds = 0.3
        await worker.reply([{"s": 1, "text": "one"}])
        await asyncio.sleep(0.1)
        await worker.reply([{"s": 1, "text": "two"}])  # resets the idle timer
        await asyncio.sleep(0.2)
        check("busy worker stays loaded", worker.proc is not None, True)
        await asyncio.sleep(0.4)
        check("idle worker unloaded", worker.proc, None)
        out = await worker.reply([{"s": 1, "text": "after idle"}])
        check("reloads after idle", out.get("text"), "echo after idle")
        worker.idle_seconds = 300.0

        # The handler end to end: tagged question in, reply out with the button and no pings.
        gpt.WORKER = worker
        bot = SimpleNamespace(user=SimpleNamespace(id=BOT))
        m = Message(ALICE, "TomCatGPT, hi there")
        check("handled", await gpt.maybe_handle(m, bot), True)
        check("answer", m.replies, ["echo hi there"])
        m2 = Message(ALICE, "TomCat, show me microwave")
        check("command not handled", await gpt.maybe_handle(m2, bot), False)
    finally:
        gpt.asyncio.create_subprocess_exec = real_exec
        await worker.stop()
        if worker.log:
            worker.log.close()


if sys.platform == "win32":  # something in the import chain picks the selector loop, which has no subprocesses
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
asyncio.run(worker_checks())

if FAILURES:
    print("\n".join(FAILURES))
    sys.exit(f"{len(FAILURES)} TomCatGPT check(s) failed")
print("TomCatGPT routing checks passed")
