"""TomCatGPT: the TomCat language model (MuonKahuna, best of 4) answering in Discord.

    "TomCatGPT, what's up?"        -> the model answers that one message
    reply (Discord reply button) to a TomCatGPT answer -> the model answers with the whole reply thread as context
                                      (each message's parent, back to the tagged message; the model keeps as many
                                      recent messages as fit its 512-token window)

The call tag is separate from the "TomCat," command wake word: on_message checks it first, so "TomCatGPT, show me
microwave" goes to the model, not the photo command.

The model runs in a worker process (tomcat/lm/worker.py) started on first use and kept loaded; this module never
imports torch. One request is answered at a time.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

import discord

from ..config import settings
from ..logger import log_action

ROOT = Path(__file__).resolve().parents[2]
MAX_THREAD = 20          # messages handed to the model (it trims to its 512-token window)
MAX_HOPS = 40            # reply-chain lookups before giving up
REQUEST_TIMEOUT = 120.0  # seconds per reply, load included
LOAD_TIMEOUT = 300.0

# "TomCatGPT", "Tomcat GPT", "tom cat gpt", "TomCat-GPT", "tomkatgpt", "TomGPT", "tomcat gtp" ... at the start of
# the message, optionally after "@", "hey" or "yo", followed by punctuation, space or the end.
_TAG = re.compile(
    r"^\s*[@!/]?\s*(?:(?:hey|hi|yo|ok|okay)[\s,]+)?"
    r"tom[\s._-]*(?:cat|kat|car)?[\s._-]*(?:g\.?\s*p\.?\s*t\.?|gtp|gbt|gpy|gp)(?:[\s_-]*\d+(?:\.\d+)?)?"
    r"(?![a-z0-9])[\s,:;.!?~*\-–—]*",
    re.I)
# One-token typos the pattern misses ("tomactgpt", "tocmatgpt"): letters only, ends in gpt, rest close to tomcat.
_FUZZY_HEADS = ("tomcat", "tomkat", "tom")


def _edit_distance(a: str, b: str) -> int:
    """Edits between a and b, a swap of two neighbouring letters ("tomact") counting as one."""
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def strip_call_tag(text: str) -> Optional[str]:
    """The message without its TomCatGPT call tag, or None when it has no tag."""
    text = text or ""
    m = _TAG.match(text)
    if m:
        return text[m.end():].strip()
    first = re.match(r"^\s*[@!/]?\s*([A-Za-z]+)(?![A-Za-z0-9])[\s,:;.!?~*\-–—]*", text)
    if first:
        word = first.group(1).lower()
        if len(word) > 3 and word.endswith(("gpt", "gtp")):
            head = word[:-3]
            if len(head) >= 5 and any(_edit_distance(head, h) <= 1 for h in _FUZZY_HEADS):
                return text[first.end():].strip()
    return None


# ---- message text as the model saw it in training (lm_build_corpus.clean) ----
_CUSTOM = re.compile(r"<a?:(\w+):(\d+)>")


def clean_text(text: str) -> str:
    text = _CUSTOM.sub(lambda m: f":{m.group(1)}:", text or "")
    text = re.sub(r"<@[!&]?\d+>", "@user", text)
    text = re.sub(r"<#\d+>", "#channel", text)
    text = re.sub(r"<t:\d+(?::\w)?>", "", text)
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"(.)\1{11,}", lambda m: m.group(1) * 12, text)
    return re.sub(r"[ \t]+", " ", text).strip()[:2000]


def _has_image(message: Any) -> bool:
    return any((getattr(a, "content_type", "") or "").startswith("image/")
               for a in (getattr(message, "attachments", None) or []))


def _bot_id(bot: Any) -> Optional[int]:
    user = getattr(bot, "user", None)
    return getattr(user, "id", None) or getattr(settings, "bot_user_id", None)


async def _parent(message: Any) -> Optional[Any]:
    """The message this one replies to (Discord reply button), from the cache or the API; None if none/deleted."""
    ref = getattr(message, "reference", None)
    if ref is None or getattr(ref, "message_id", None) is None:
        return None
    resolved = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
    if resolved is not None and hasattr(resolved, "content"):
        return resolved
    channel = getattr(message, "channel", None)
    if getattr(ref, "channel_id", None) not in (None, getattr(channel, "id", None)):
        return None
    try:
        return await channel.fetch_message(ref.message_id)
    except Exception:
        return None


async def build_thread(message: Any, bot_id: Optional[int]) -> Optional[List[dict]]:
    """[{"s", "text", "img"?}] for the model, oldest first, or None when this message is not for TomCatGPT.

    A tagged message starts (or, as a reply to a TomCatGPT answer, continues) a thread. An untagged message
    counts only as a reply to one of the bot's answers whose chain goes back to a tagged message, so replies to
    the bot's ordinary command answers are left to the router. The bot is speaker 2, people are 1, 3, 4, ...
    in order of appearance.
    """
    def author_of(msg):
        return getattr(getattr(msg, "author", None), "id", None)

    own = strip_call_tag(getattr(message, "content", "") or "")
    parent = await _parent(message)
    if own is None and (parent is None or author_of(parent) != bot_id):
        return None
    # Walk up: person, bot, person, bot, ... The thread reaches back to the furthest tagged person message
    # (root); anything past it that breaks the pattern is cut off.
    chain = [(message, own if own is not None else (message.content or ""))]
    root_len = 1 if own is not None else 0
    node, hops = parent, 0
    while node is not None and hops < MAX_HOPS:
        hops += 1
        if author_of(node) == bot_id:
            chain.append((node, node.content or ""))
        else:
            tagged = strip_call_tag(getattr(node, "content", "") or "")
            chain.append((node, tagged if tagged is not None else (node.content or "")))
            if tagged is not None:
                root_len = len(chain)
        expect_bot = author_of(node) != bot_id
        node = await _parent(node)
        if node is None or (author_of(node) == bot_id) != expect_bot:
            break
    else:
        if node is not None:  # still person/bot/person/bot after MAX_HOPS: a long thread whose root is far back
            root_len = len(chain)
    if not root_len:
        return None  # a reply to an ordinary bot answer (a "TomCat," command), not a TomCatGPT thread
    chain = chain[:root_len]
    chain.reverse()
    speakers: dict = {}
    msgs = []
    for msg, text in chain[-MAX_THREAD:]:
        author = getattr(getattr(msg, "author", None), "id", None)
        if author == bot_id:
            s = 2
        else:
            if author not in speakers:
                speakers[author] = 1 if not speakers else len(speakers) + 2
                if speakers[author] > 8:
                    speakers[author] = 8
            s = speakers[author]
        item = {"s": s, "text": clean_text(text)}
        if s != 2 and _has_image(msg):
            item["img"] = True
        if item["text"] or item.get("img"):
            msgs.append(item)
    while msgs and msgs[0]["s"] == 2:
        msgs.pop(0)
    if not msgs or msgs[-1]["s"] == 2:
        return []
    return msgs


class Worker:
    """The worker process and a lock so one request is in flight at a time."""

    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()
        self.next_id = 0
        self.log = None

    def available(self) -> bool:
        return (ROOT / settings.tomcatgpt_dir / "manifest.json").is_file()

    async def _start(self) -> None:
        bundle = ROOT / settings.tomcatgpt_dir
        log_path = bundle / "worker.log"
        if self.log is None or self.log.closed:
            self.log = open(log_path, "ab")  # noqa: SIM115 - kept open for the worker's stderr
        started = time.perf_counter()
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tomcat.lm.worker", str(bundle), "--threads", str(settings.tomcatgpt_threads),
            cwd=str(ROOT), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log)
        line = await asyncio.wait_for(self.proc.stdout.readline(), LOAD_TIMEOUT)
        ready = json.loads(line or b"{}")
        if not ready.get("ready"):
            await self.stop()
            raise RuntimeError(ready.get("error") or "TomCatGPT worker exited while loading")
        log_action("tomcatgpt_worker_ready", f"pid={self.proc.pid}",
                   f"load={ready.get('load_seconds')}s; wait={time.perf_counter() - started:.1f}s")

    async def ensure(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            await self._start()

    async def warm(self) -> None:
        """Load the model in the background at startup, so the first question is not slow."""
        if not settings.tomcatgpt_enabled or not self.available():
            return
        async with self.lock:
            try:
                await self.ensure()
            except Exception as e:
                log_action("tomcatgpt_worker_error", "warm", f"{type(e).__name__}: {e}")

    async def reply(self, messages: List[dict]) -> dict:
        async with self.lock:
            for attempt in (1, 2):  # a dead worker (e.g. OOM-killed) gets one restart
                await self.ensure()
                self.next_id += 1
                request = json.dumps({"id": self.next_id, "messages": messages}, ensure_ascii=False) + "\n"
                try:
                    self.proc.stdin.write(request.encode("utf-8"))
                    await self.proc.stdin.drain()
                    line = await asyncio.wait_for(self.proc.stdout.readline(), REQUEST_TIMEOUT)
                except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError) as e:
                    await self.stop()
                    if attempt == 2:
                        raise RuntimeError(f"TomCatGPT worker failed: {type(e).__name__}") from e
                    continue
                if not line:
                    await self.stop()
                    if attempt == 2:
                        raise RuntimeError("TomCatGPT worker exited")
                    continue
                return json.loads(line)
        raise RuntimeError("unreachable")

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass


WORKER = Worker()


async def maybe_handle(message: Any, bot: Any) -> bool:
    """Answer the message if it is for TomCatGPT; True when it was (the caller then skips the normal router)."""
    if not settings.tomcatgpt_enabled:
        return False
    text = getattr(message, "content", "") or ""
    if strip_call_tag(text) is None and getattr(message, "reference", None) is None:
        return False  # the common case, decided without any lookup
    bot_id = _bot_id(bot)
    try:
        thread = await build_thread(message, bot_id)
    except Exception as e:
        log_action("tomcatgpt_thread_error", f"msg={getattr(message, 'id', '?')}", f"{type(e).__name__}: {e}")
        return False
    if thread is None:
        return False
    channel = message.channel
    if not thread:
        await _send(message, "Ask me something after my name, like `TomCatGPT, who's the chonkiest cat?`")
        return True
    if not WORKER.available():
        log_action("tomcatgpt_unavailable", f"dir={settings.tomcatgpt_dir}", "no bundle")
        await _send(message, "TomCatGPT isn't installed on this server yet.")
        return True
    try:
        async with channel.typing():
            result = await WORKER.reply(thread)
    except Exception as e:
        log_action("tomcatgpt_error", f"msg={message.id}", f"{type(e).__name__}: {e}")
        await _send(message, "TomCatGPT is napping right now. Try again in a minute.")
        return True
    if result.get("error") or not (result.get("text") or "").strip():
        log_action("tomcatgpt_error", f"msg={message.id}", str(result.get("error") or "empty reply"))
        await _send(message, "I couldn't think of anything to say. Try asking another way?")
        return True
    log_action("tomcatgpt_reply", f"msg={message.id}; ch={getattr(channel, 'id', '?')}",
               f"thread={len(thread)}; candidates={result.get('candidates')}; seconds={result.get('seconds')}")
    await _send(message, result["text"])
    return True


async def _send(message: Any, text: str) -> None:
    """Reply with the Discord reply button (so people can continue the thread) and never ping anyone."""
    if getattr(settings, "silent_mode", False):
        log_action("send_suppressed", f"ch={getattr(message.channel, 'id', None)}", text[:120])
        return
    try:
        await message.reply(text[:2000], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        log_action("tomcatgpt_send_error", f"msg={getattr(message, 'id', '?')}", f"{type(e).__name__}: {e}")
