"""Server vocabulary for the TomCat language model (vendored from scripts/lm_tokenizer.py, runtime part only).

Plain Python, no third-party packages. Keep in sync with the research copy: the deployed checkpoints were
trained with that tokenizer, and tomcat/lm checks the vocabulary hash against the bundle at load time.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SP = "▁"

_BASE = ("©®‼⁉™ℹ↔-↪⌚-⏿Ⓜ▪-◾☀-➿"
         "⤴⤵⬅-⭕〰〽㊗㊙\U0001F000-\U0001F1E5\U0001F200-\U0001FAFF")
_UNIT = f"[{_BASE}][\ufe0e\ufe0f]?[\U0001F3FB-\U0001F3FF]?"
EMOJI = f"(?:[0-9#*][\ufe0e\ufe0f]?⃣|[\U0001F1E6-\U0001F1FF]{{2}}|{_UNIT}(?:‍{_UNIT})*(?:[\U000e0020-\U000e007e]+\U000e007f)?)"
ATOM = re.compile(
    r"(?P<special><url>|<xemoji>|@user|#channel)"
    r"|(?P<custom>:\w+:)"
    f"|(?P<emoji>{EMOJI})"
    r"|(?P<word>[^\W_]+(?:['’][^\W_]+)*)"
    r"|(?P<nl>\n)"
    r"|(?P<space> +)"
    r"|(?P<other>.)", re.S)
CASE_SEG = re.compile(r"[A-Z]+(?![a-z])|[A-Z][^A-Z]*|[^A-Z]+")
BYTE = re.compile(r"<0x([0-9a-f]{2})>")

FIXED = ["<pad>", "<bos>", "<eos>", "<unk>", "<eom>", "<nl>", SP, "<cap>", "<caps>", "<low>", "<title>", "<mixed>", "<re>", "<photo>",
         "<img>", "<url>", "<xemoji>", "@user", "#channel", "<emoji_rare>", "<cats>", "<#other>"]
FIXED += [f"<0x{i:02x}>" for i in range(256)]
FIXED += [f"<upper{i}>" for i in range(128)]
SLOTS = [f"<s{i}>" for i in range(1, 9)]
YEARS = [f"<y{y}>" for y in range(2019, 2029)]
# Optional markers for the text a reply quotes (msg["quote"]). Not in FIXED: a
# tokenizer without them must keep loading and tokenizing exactly as before.
# Built tokenizers append them to the end of the vocabulary, so existing ids do not move.
QUOTE_TOKENS = ("<quote>", "<quote_end>")
# Emoticons: each is one atomic token spelled exactly as typed (case kept: "xD" is not "XD").
# Each entry is independent: it is used only when its token is in the vocabulary AND atomic
# (Tokenizer.emoticons), so every legacy tokenizer, which has none, tokenizes as before.
# Builds add all of them, appended after the other atomic tokens. An emoticon is matched
# only when it stands alone (start or whitespace before, end or whitespace after), so
# "(see this):", "3:00" and "a:)b" are unchanged. Longest first.
EMOTICONS = (
    ":-)", ";-)", ":-(", "</3", ":')", ":'(", ">:(", "^_^", "^-^", "-_-", ">_<", "T_T", ";_;", "._.", ":^)",
    ":)", "(:", "):", ":(", ":D", "D:", ":P", ":p", ":3", "c:", ":c", ";)", ":/", ":|", ":O", ":o",
    "xD", "XD", "xd", "<3", "^^", "o7", ":]", ":[", "=)", "=(",
)
# Tone indicators: same rules as EMOTICONS (one atomic token, standalone, exact case). Their
# leading "/" never appears in a word, so no learned piece can spell one.
TONE_INDICATORS = (
    "/lhj", "/nsrs", "/genq", "/gen", "/srs", "/sar", "/ref", "/pos", "/neg", "/aff", "/lyr", "/hyp", "/nbh",
    "/lh", "/hj", "/jk", "/nm", "/af", "/rh", "/li", "/ex", "/th", "/j", "/s", "/p", "/t", "/c", "/l", "/m", "/f",
)
# Name pointers: one token each, matched anywhere in text (like <url>), but only when the
# tokenizer has all of them (Tokenizer.names). Legacy text such as "<name_9>" is not special.
NAME_TOKENS = tuple(f"<name_{i}>" for i in range(1, 9)) + ("<name_other>",)
NAME_SPLIT = re.compile(r"(<name_(?:[1-8]|other)>)")


def segments(word: str) -> list[tuple[str, str]]:
    """Split a word into (case marker, lowercase text) parts."""
    def kind(s: str) -> str:
        if s == s.lower():
            return ""
        if s == s.upper() and sum(c.isalpha() for c in s) > 1:
            return "<caps>"
        return "<cap>" if s[0].isupper() and s[1:] == s[1:].lower() else "?"
    k = kind(word)
    if k != "?":
        return [(k, word.lower())]
    out = []
    for i, seg in enumerate(CASE_SEG.findall(word)):
        k = kind(seg)
        k = "<cap>" if k == "?" else k
        out.append((k or ("<low>" if i else ""), seg.lower()))
    return out


class Tokenizer:
    def __init__(self, data: dict):
        self.data = data
        self.vocab: list[str] = data["vocab"]
        self.id = {t: i for i, t in enumerate(self.vocab)}
        if any(t not in self.id for t in FIXED):
            raise ValueError("Tokenizer has an obsolete marker vocabulary; retrain it before use")
        self.atomic = set(data["atomic"])        # tokens that are not word pieces
        present = [t for t in QUOTE_TOKENS if t in self.id]
        if present and len(present) != len(QUOTE_TOKENS):
            raise ValueError("Tokenizer has only one of the quote markers")
        if present and not all(t in self.atomic for t in QUOTE_TOKENS):
            raise ValueError("Quote markers must be atomic tokens")
        self.quotes = bool(present)              # quote text is emitted only with these markers
        present = [t for t in NAME_TOKENS if t in self.id]
        if present and len(present) != len(NAME_TOKENS):
            raise ValueError("Tokenizer has only some of the name markers")
        if present and not all(t in self.atomic for t in NAME_TOKENS):
            raise ValueError("Name markers must be atomic tokens")
        self.names = bool(present)
        self.name_re = NAME_SPLIT if self.names else None
        self.emoticons = tuple(e for e in EMOTICONS if e in self.id and e in self.atomic)
        self.tone_indicators = tuple(t for t in TONE_INDICATORS if t in self.id and t in self.atomic)
        # One pattern for every standalone atom; longest first so the longer spelling wins.
        specials = sorted(set(self.emoticons) | set(self.tone_indicators), key=len, reverse=True)
        self.standalone = (re.compile(r"(?<!\S)(?:" + "|".join(re.escape(e) for e in specials)
                                      + r")(?!\S)") if specials else None)
        self.rank = {tuple(p): i for i, p in enumerate(data["merges"])}
        self.protected = set(data["protected"])  # whole-word tokens (cat names)
        self.custom = {e["name"]: e for e in data["custom_emoji"]}
        self.chars = set(data["chars"])
        self._cache: dict[str, list[str]] = {}
        self._curly_cache: dict[tuple[str, bool], list[str]] = {}
        self.cat_pattern = re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(self.protected, key=len, reverse=True))
            + r")(?!\w)", re.I) if self.protected else None

    # ---- text -> tokens -------------------------------------------------
    def _bpe(self, word: str) -> list[str]:
        """word may start with the space mark; returns its pieces."""
        got = self._cache.get(word)
        if got is not None:
            return got
        if word in self.protected or (word[:1] == SP and word[1:] in self.protected):
            parts = [word]
        else:
            if any(c not in self.chars and c != SP for c in word):
                return ([SP] if word.startswith(SP) else []) + self._bytes(word.lstrip(SP))
            parts = list(word)
            if word[:1] == SP and len(parts) > 1:
                parts[:2] = [parts[0] + parts[1]] if parts[1] != "<unk>" else parts[:2]
            while len(parts) > 1:
                best = min(range(len(parts) - 1), key=lambda i: self.rank.get((parts[i], parts[i + 1]), 1 << 30))
                pair = (parts[best], parts[best + 1])
                if pair not in self.rank:
                    break
                merged, i = [], 0
                while i < len(parts):
                    if i < len(parts) - 1 and (parts[i], parts[i + 1]) == pair:
                        merged.append(parts[i] + parts[i + 1])
                        i += 2
                    else:
                        merged.append(parts[i])
                        i += 1
                parts = merged
            parts = [p if p in self.id else "<unk>" for p in parts]
        if len(self._cache) < 500000:
            self._cache[word] = parts
        return parts

    def _curly(self, word: str, spaced: bool) -> list[str] | None:
        """Pieces for a word with curly apostrophes, or None if that would not decode exactly."""
        got = self._curly_cache.get((word, spaced))
        if got is not None:
            return got or None
        parts = word.split("’")
        low = word.lower()
        mark = ("" if word == low else
                "<caps>" if word == word.upper() and sum(c.isalpha() for c in word) > 1 else
                "<cap>" if word == word[:1].upper() + low[1:] else None)
        out = None
        if mark is not None and all(p.isascii() and all(c in self.chars for c in p.lower()) for p in parts):
            out = [mark] if mark else []
            for i, part in enumerate(parts):
                if i:
                    out.append("’")
                out += self._bpe((SP if spaced and i == 0 else "") + part.lower())
            if "<unk>" in out or self.decode(out) != (" " if spaced else "") + word:
                out = None
        if len(self._curly_cache) < 200000:
            self._curly_cache[(word, spaced)] = out or []
        return out

    @staticmethod
    def _bytes(text: str) -> list[str]:
        return [f"<0x{b:02x}>" for b in text.encode("utf-8")]

    def tokens(self, text: str) -> list[str]:
        out: list[str] = []
        space = 0
        pos = 0
        while pos < len(text):
            # Whole-text atoms checked before ATOM: standalone emoticons and tone indicators,
            # and name pointers (which may sit inside text). Each is one token, spaced like an emoji.
            emo = self.standalone.match(text, pos) if self.standalone else None
            if emo is None and self.name_re:
                emo = self.name_re.match(text, pos)
            if emo:
                out.extend([SP] * space)
                out.append(emo.group())
                space = 0
                pos = emo.end()
                continue
            m = ATOM.match(text, pos)
            assert m is not None
            kind, s = m.lastgroup, m.group(0)
            cat = self.cat_pattern.match(text, pos) if self.cat_pattern and kind == "word" else None
            if cat and cat.group().isascii() and len(cat.group()) <= 128:
                s = cat.group()
                pos = cat.end()
                mark = ("" if s == s.lower() else "<caps>" if s == s.upper() else
                        "<title>" if s == s.title() and s != s.capitalize() else
                        "<cap>" if s == s.capitalize() else "<mixed>")
                out.extend([SP] * max(0, space - 1))
                if mark:
                    out.append(mark)
                if mark == "<mixed>":
                    out.extend(f"<upper{i}>" for i, c in enumerate(s) if c.isupper())
                out += self._bpe((SP if space else "") + s.lower())
                space = 0
                continue
            pos = m.end()
            if kind == "space":
                space += len(s)
                continue
            out.extend([SP] * max(0, space - (1 if kind == "word" else 0)))
            if kind == "word":
                if "’" in s and "’" in self.id:
                    # Phone keyboards type a curly apostrophe (don’t, i’m). Keep it
                    # as typed, but as word pieces around one apostrophe token
                    # rather than spelling the whole word out in bytes.
                    curly = self._curly(s, bool(space))
                    if curly is not None:
                        out += curly
                        space = 0
                        continue
                if not s.isascii() or any(c not in self.chars for c in s.lower()):
                    if space:
                        out.append(SP)
                    out += self._bytes(s)
                    space = 0
                    continue
                for i, (mark, low) in enumerate(segments(s)):
                    if mark:
                        out.append(mark)
                    out += self._bpe((SP if space and i == 0 else "") + low)
            else:
                if kind == "nl":
                    out.append("<nl>")
                elif kind == "custom":
                    out.append(s if s in self.id else "<xemoji>")
                elif kind == "emoji":
                    out.append(s if s in self.id else "<emoji_rare>")
                else:
                    out.extend([s] if s in self.id else self._bytes(s))
            space = 0
        out.extend([SP] * space)
        return out

    def encode(self, text: str) -> list[int]:
        return [self.id[t] for t in self.tokens(text)]

    def session_tokens(self, session: dict, tags: bool = True, known_cats: list[str] | None = None) -> list[str]:
        """A whole conversation, optionally steered by cats known before it.

        The corpus's session-wide cats include later messages and photos; using
        them as a prefix leaks the future. Only an explicit caller-supplied
        ``known_cats`` list can provide cat steering. tags=False suppresses both
        channel and cat tags for steering dropout.
        """
        out = ["<bos>", f"<y{session['year']}>"]
        if tags:
            ch = f"<#{session['channel']}>"
            out.append(ch if ch in self.id else "<#other>")
            if known_cats:
                out.append("<cats>")
                for c in known_cats:
                    out += self._bpe(SP + c.lower())
        for m in session["msgs"]:
            out.append(f"<s{m['s']}>")
            if m.get("re"):
                out += ["<re>", f"<s{m['re']}>"]
            # The replied-to text (first words of the parent), context only: the
            # trainer never scores tokens between <quote> and <quote_end>.
            if self.quotes and m.get("quote"):
                quoted = self.tokens(m["quote"])
                if quoted:
                    out += ["<quote>", *quoted, "<quote_end>"]
            # An attachment belongs to the same message as the text. Put it
            # after the text so a causal model can choose whether to attach a
            # cat photo based on the response it has just generated. Later
            # messages can still attend to both the text and its photo label.
            out += self.tokens(m["text"])
            if m.get("photo"):
                out.append("<photo>")
                for c in m["photo"]:
                    out += self._bpe(SP + c)
            elif m.get("img"):
                out.append("<img>")
            out.append("<eom>")
        return out + ["<eos>"]

    # ---- tokens -> text -------------------------------------------------
    def decode(self, toks: list[str] | list[int], discord: bool = False) -> str:
        """discord=True writes custom emoji in the form Discord needs from a bot."""
        toks = [self.vocab[t] if isinstance(t, int) else t for t in toks]
        out: list[str] = []
        raw = bytearray()
        def flush_bytes() -> None:
            if raw:
                out.append(raw.decode("utf-8", errors="replace"))
                raw.clear()
        pending = current = ""
        upper: set[int] = set()
        in_word = False
        for t in toks:
            byte = BYTE.fullmatch(t)
            if byte:
                raw.append(int(byte.group(1), 16))
                continue
            flush_bytes()
            if t in ("<cap>", "<caps>", "<low>", "<title>", "<mixed>"):
                pending, in_word = t, False
                upper.clear()
                continue
            if pending == "<mixed>" and t.startswith("<upper") and t.endswith(">"):
                index = t[6:-1]
                if index.isdigit():
                    upper.add(int(index))
                continue
            if t in self.atomic:
                if t in ("'", "’") and in_word:
                    out.append(t)
                    continue
                in_word = False
                pending = ""
                if t == SP:
                    out.append(" ")
                elif t == "<nl>":
                    out.append("\n")
                elif discord and t[1:-1] in self.custom and t.startswith(":"):
                    e = self.custom[t[1:-1]]
                    out.append(f"<{'a' if e['animated'] else ''}:{e['name']}:{e['id']}>")
                else:
                    out.append(t)
                continue
            lead, body = (" ", t[1:]) if t.startswith(SP) else ("", t)
            if lead or not in_word:
                current, pending = pending, ""
                if current == "<cap>":
                    body = body[:1].upper() + body[1:]
            if current == "<caps>":
                body = body.upper()
            elif current == "<title>":
                body = body.title()
            elif current == "<mixed>":
                body = "".join(c.upper() if i in upper else c for i, c in enumerate(body))
            out.append(lead + body)
            in_word = True
        flush_bytes()
        return "".join(out)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Tokenizer":
        return cls(json.loads(path.read_text(encoding="utf-8")))
