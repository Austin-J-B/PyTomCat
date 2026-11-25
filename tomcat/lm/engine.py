"""TomCatGPT inference on CPU: the dashboard's MuonKahuna chat, best of 4.

A runtime copy of the research path (scripts/lm_watch.chat_reply with lm_sample's steered sampler and
lm_reply_judge's reviewer), cut down to what one model needs and free of the research imports. The weights
come from a bundle folder written by scripts/export_tomcatgpt_bundle.py:

    generator.pt   the chat model (muon_u1_bo16)              {"config", "untied", "state_dict", "step"}
    judge.pt       the reviewer (judge_mk_v3) and its frozen   {"config", "untied", "lm", "head", "hidden",
                   base model for the fit features              "classes", "feats", "ref", "ref_untied"}
    tokenizer.json the vocabulary both were trained with
    blocklist/     member_full_names.json, slur_patterns.json, name_stems.json (private; never in git)
    manifest.json  sha256 of each file, checked at load

How a reply is picked (MuonKahuna's CHAT_SETTINGS on the dashboard, 2026-11-24): 4 candidates sampled
together with context-aware decoding (contrast 1.0, T 0.6, top-p 0.9, max 80 tokens), copies of any chat
message dropped, then the best by
    judge / 0.38 + 0.5 ln(distinct words) - 2 x share of words the bot already said
    - 0.5 x echo of the last 3 messages - 3 x self-repeat share - 3 if it names a cat the chat didn't.
A message that is only cat sounds gets 32 candidates and a bonus for meowing back.

This module imports torch: only the worker process (tomcat/lm/worker.py) may import it, never the bot.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F

from .tokenizer import SP, Tokenizer

# ---- picker settings (dashboard MuonKahuna) ---------------------------------------------------------------
CANDIDATES = 4
FALLBACK_CANDIDATES = 8  # every one of the 4 was a repeat: one full batch, like the dashboard
MEOW_CANDIDATES = 32
SAMPLE_CHUNK = 8  # rows sampled per pass (bounds the key/value cache memory)
TEMPERATURE, TOP_P, MAX_TOKENS, CONTRAST, PLAUSIBLE = 0.6, 0.9, 80, 1.0, 0.1
JUDGE_SPREAD = 0.38       # judge_mk_v3's score spread over model replies; pick uses judge / spread
LENGTH_BONUS = 0.5
ECHO_PENALTY = 0.5
SELF_REPEAT_PENALTY = 3.0
NEW_CAT_PENALTY = 3.0
REPEAT_PENALTY = 2.0
MEOW_BONUS = 2.0
CHANNEL = "cats-on-campus"
BOT_SPEAKER = 2
MAX_MESSAGES = 20
MAX_CHARS = 2000

STOPWORDS = frozenset(
    "the and you your that this what for are was with have not but can its it's just like get got she him her "
    "his they them their there then than too yes yeah yep nah nope lol lmao idk okay any all one how why who "
    "when where out now about our some been from will would could should did does dont don't i'm im".split())
MEOW_WORD = r"\W*(?:m+e*o+w+|m+r{2,}p*|m+r+p+|nya+|mew+|pur+)\W*"
MEOW_ONLY = re.compile(rf"^{MEOW_WORD}(?:\s+{MEOW_WORD})*$", re.I)

# Tokens the bot must never write: anything that could ping, control markers, grade tags, and the generic
# <img> marker (the bot has no picture to attach).
NEVER = ("<pad>", "<bos>", "<xemoji>", "<cats>", "<reply_target>", "<quote>", "<quote_end>", "@user", "@", "<0x40>",
         "<0x01>", "<0x02>", "<0x03>", "<0x04>", "<0x05>", "<img>")


# ---- model (pretrain_ccc_causal.CausalLM, inference only) ------------------------------------------------
@dataclass
class CausalConfig:
    vocab_size: int = 1024
    width: int = 192
    heads: int = 4
    layers: int = 3
    feedforward: int = 512
    max_length: int = 128
    dropout: float = 0.1
    variant: str = "classic"


class RMSNorm(nn.Module):
    def __init__(self, width, eps=1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(width))
        self.eps = eps

    def forward(self, x):
        scale = torch.rsqrt(x.float().square().mean(-1, keepdim=True) + self.eps).to(x.dtype)
        return x * scale * self.weight


def rotary_factors(length, width):
    inverse = torch.exp(-math.log(10000.) * torch.arange(0, width, 2, dtype=torch.float32) / width)
    angles = torch.arange(length, dtype=torch.float32)[:, None] * inverse[None, :]
    return angles.cos(), angles.sin()


def rotary(x, factors):
    """Interleaved RoPE on [batch, heads, sequence, head_width]."""
    width = x.shape[-1]
    cosine, sine = factors
    cosine, sine = cosine.to(x.dtype), sine.to(x.dtype)
    first, second = x.reshape(*x.shape[:-1], width // 2, 2).unbind(-1)
    return torch.stack((first * cosine - second * sine, first * sine + second * cosine), dim=-1).flatten(-2)


class CausalBlock(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.heads = cfg.heads
        self.modern = cfg.variant == "modern"
        self.norm1 = RMSNorm(cfg.width) if self.modern else nn.LayerNorm(cfg.width)
        self.norm2 = RMSNorm(cfg.width) if self.modern else nn.LayerNorm(cfg.width)
        self.qkv = nn.Linear(cfg.width, 3 * cfg.width, bias=not self.modern)
        self.proj = nn.Linear(cfg.width, cfg.width, bias=not self.modern)
        if self.modern:
            gated = max(8, round((cfg.feedforward * 2 / 3) / 8) * 8)
            self.gate = nn.Linear(cfg.width, gated, bias=False)
            self.up = nn.Linear(cfg.width, gated, bias=False)
            self.down = nn.Linear(gated, cfg.width, bias=False)
        else:
            self.up, self.down = nn.Linear(cfg.width, cfg.feedforward), nn.Linear(cfg.feedforward, cfg.width)

    def mlp(self, x):
        hidden = self.norm2(x)
        hidden = F.silu(self.gate(hidden)) * self.up(hidden) if self.modern else F.gelu(self.up(hidden))
        return x + self.down(hidden)

    def forward(self, x, factors=None):
        b, n, w = x.shape
        q, k, v = self.qkv(self.norm1(x)).view(b, n, 3, self.heads, w // self.heads).unbind(2)
        q, k, v = (t.transpose(1, 2) for t in (q, k, v))
        if self.modern:
            q, k = rotary(q, factors), rotary(k, factors)
        a = F.scaled_dot_product_attention(q, k, v, is_causal=True)
        return self.mlp(x + self.proj(a.transpose(1, 2).reshape(b, n, w)))


class CausalLM(nn.Module):
    def __init__(self, cfg, pad, untied=False):
        super().__init__()
        self.cfg = cfg
        self.token = nn.Embedding(cfg.vocab_size, cfg.width, padding_idx=pad)
        self.position = nn.Embedding(cfg.max_length, cfg.width) if cfg.variant == "classic" else None
        if cfg.variant == "modern":
            cosine, sine = rotary_factors(cfg.max_length, cfg.width // cfg.heads)
            self.register_buffer("rope_cosine", cosine, persistent=False)
            self.register_buffer("rope_sine", sine, persistent=False)
        self.blocks = nn.ModuleList(CausalBlock(cfg) for _ in range(cfg.layers))
        self.norm = RMSNorm(cfg.width) if cfg.variant == "modern" else nn.LayerNorm(cfg.width)
        self.bias = nn.Parameter(torch.zeros(cfg.vocab_size))
        self.output = nn.Linear(cfg.width, cfg.vocab_size, bias=False) if untied else None

    @property
    def out_weight(self):
        return self.output.weight if self.output is not None else self.token.weight

    def factors(self, start, steps):
        if self.cfg.variant != "modern":
            return None
        return self.rope_cosine[start:start + steps], self.rope_sine[start:start + steps]

    def embed(self, ids, start=0):
        x = self.token(ids)
        if self.position is not None:
            x = x + self.position(torch.arange(start, start + ids.shape[1]))[None]
        return x

    def hidden_states(self, ids):
        x = self.embed(ids)
        factors = self.factors(0, ids.shape[1])
        for block in self.blocks:
            x = block(x, factors)
        return self.norm(x)


def build_model(config: dict, untied: bool, state: dict, pad: int) -> CausalLM:
    """assign=True uses the loaded tensors as the weights instead of copying them in: with torch.load(mmap=True)
    they stay file-backed pages, which the kernel can drop and re-read, rather than a second private copy.
    The layers skip their random initialisation: the placeholder weights stay untouched (never resident) until the
    loaded ones replace them, instead of ~216 MB per model written for a moment at load. (The meta device would do
    the same but its first use costs ~70 MB of memory for good.)"""
    linear, embedding = nn.Linear.reset_parameters, nn.Embedding.reset_parameters
    nn.Linear.reset_parameters = nn.Embedding.reset_parameters = lambda self: None
    try:
        model = CausalLM(CausalConfig(**config), pad, untied)
    finally:
        nn.Linear.reset_parameters, nn.Embedding.reset_parameters = linear, embedding
    model.load_state_dict(state, assign=True)
    return model.eval().requires_grad_(False)


# ---- sampling (lm_sample.PromptRows / generate_replies_steered) --------------------------------------------
class PromptRows:
    """n sampling rows sharing one prompt: the prompt runs once, its keys/values are copied to every row, then
    step() adds one token per row. .logits holds the latest next-token logits [n, vocab]."""

    def __init__(self, model: CausalLM, ids: list[int], n: int, new_tokens: int = MAX_TOKENS):
        cfg = model.cfg
        self.model, self.heads, self.width = model, cfg.heads, cfg.width
        self.head = self.width // self.heads
        length = min(cfg.max_length, len(ids) + new_tokens)  # only these positions are ever written
        self.k = torch.zeros((len(model.blocks), n, self.heads, length, self.head))
        self.v = torch.zeros_like(self.k)
        self.logits = self._run(torch.tensor([ids]), 0).expand(n, -1)
        self.k[:, 1:] = self.k[:, :1]
        self.v[:, 1:] = self.v[:, :1]
        self.position = len(ids)

    def step(self, token):
        self.logits = self._run(token[:, None], self.position)
        self.position += 1
        return self.logits

    def _run(self, ids, start):
        model, heads, head, width = self.model, self.heads, self.head, self.width
        rows, steps = ids.shape
        x = model.embed(ids, start)
        factors = model.factors(start, steps)
        for layer, block in enumerate(model.blocks):
            q, k, v = block.qkv(block.norm1(x)).view(rows, steps, 3, heads, head).unbind(2)
            q, k, v = (t.transpose(1, 2) for t in (q, k, v))
            if block.modern:
                q, k = rotary(q, factors), rotary(k, factors)
            self.k[layer, :rows, :, start:start + steps] = k
            self.v[layer, :rows, :, start:start + steps] = v
            keys, values = self.k[layer, :rows, :, :start + steps], self.v[layer, :rows, :, :start + steps]
            a = F.scaled_dot_product_attention(q, keys, values, is_causal=steps > 1 and start == 0)
            x = block.mlp(x + block.proj(a.transpose(1, 2).reshape(rows, steps, width)))
        return F.linear(model.norm(x[:, -1]), model.out_weight, model.bias)


def is_speaker(token: str) -> bool:
    return len(token) == 4 and token.startswith("<s") and token.endswith(">") and token[2].isdigit()


def message_starts(tokens: list[str]) -> list[int]:
    return [i for i, t in enumerate(tokens) if is_speaker(t) and (i == 0 or tokens[i - 1] != "<re>")]


def fit(tokens: list[str], limit: int) -> list[str]:
    """Keep the header and as many whole recent messages as fit, like a training window."""
    if len(tokens) <= limit:
        return tokens
    starts = [i for i, t in enumerate(tokens) if is_speaker(t) and tokens[i - 1] != "<re>"]
    if not starts:
        return tokens[-limit:]
    header = tokens[:starts[0]]
    for start in starts:
        if len(header) + len(tokens) - start <= limit:
            return header + tokens[start:]
    return header + tokens[-(limit - len(header)):]


def bare_prefix(prefix: list[str]) -> list[str]:
    starts = message_starts(prefix)
    return prefix[:starts[0]] + [prefix[starts[-1]]] if starts else list(prefix)


def word_pattern(stems):
    body = "|".join(s for s in stems)
    return re.compile(r"(?<![^\W_])(?:" + body + r")(?![^\W_])", re.IGNORECASE) if stems else None


def is_bot_command(text: str) -> bool:
    return " ".join(text.lower().split()).lstrip("@!/ ").startswith(("tomcat", "tom cat"))


# ---- reviewer (lm_reply_judge.ReplyJudge with fit features) ------------------------------------------------
class ReplyJudge(nn.Module):
    def __init__(self, lm: CausalLM, ref: CausalLM | None, hidden: int, classes: int, feats: bool):
        super().__init__()
        self.lm, self.ref = lm, ref
        self.n_feat = 5 if feats else 0
        width = lm.cfg.width
        self.head = nn.Sequential(nn.Linear(2 * width + self.n_feat, hidden), nn.GELU(), nn.Dropout(0.1),
                                  nn.Linear(hidden, classes))


def opener_tokens(item: dict) -> list[str]:
    if item.get("re"):
        return [f"<s{item['speaker']}>", "<re>", f"<s{item['re']}>"]
    return [f"<s{item['speaker']}>"]


def judge_encode(tokenizer: Tokenizer, item: dict, reply: list[str], limit: int):
    """(ids, reply start, reply end) for conversation + opener + reply; drops the oldest messages to fit."""
    opener = opener_tokens(item)
    reply = reply[: limit // 2 - 1] + ["<eom>"] if len(reply) > limit // 2 else reply
    msgs = list(item["context"])
    while True:
        head = tokenizer.session_tokens({"year": item["year"], "channel": item["channel"], "msgs": msgs}, tags=True)[:-1]
        tokens = head + opener + reply
        if len(tokens) <= limit or len(msgs) <= 1:
            break
        msgs = msgs[1:]
    tokens = tokens[-limit:]
    return [tokenizer.id[t] for t in tokens], len(tokens) - len(reply), len(tokens) - 1


def padded(rows, pad):
    width = max(len(r) for r in rows)
    ids = torch.full((len(rows), width), pad, dtype=torch.long)
    for i, r in enumerate(rows):
        ids[i, :len(r)] = torch.tensor(r)
    return ids


def fit_features(judge: ReplyJudge, tokenizer: Tokenizer, examples: list[dict], limit: int) -> list[list[float]]:
    """Per reply, from the frozen base model: [mean log p(reply | chat), mean log p(reply | no chat), their
    difference, log p(<eom> | reply), log reply length] (lm_reply_judge.fit_features)."""
    pad, ref = tokenizer.id["<pad>"], judge.ref

    def logps(rows):
        ids = padded([r[0] for r in rows], pad)
        states = ref.hidden_states(ids[:, :-1])
        reply_states = torch.cat([states[i, s - 1:e] for i, (_, s, e) in enumerate(rows)])
        targets = torch.cat([ids[i, s:e + 1] for i, (_, s, e) in enumerate(rows)])
        lp = -F.cross_entropy(F.linear(reply_states, ref.out_weight, ref.bias).float(), targets, reduction="none")
        out, cursor = [], 0
        for _, s, e in rows:
            t = lp[cursor:cursor + e - s + 1]
            out.append((t.sum().item(), t.numel(), t[-1].item()))
            cursor += e - s + 1
        return out

    # lm_reply_judge sorts each batch by length before padding; padding sits after every real token and the
    # causal mask keeps real positions from seeing it, so the order does not change any value.
    ctx = logps([(e["ids"], e["start"], e["end"]) for e in examples])
    bare = []
    for e in examples:
        item = {**e["item"], "context": [{"s": e["item"]["re"], "text": ""}]}
        bare.append(judge_encode(tokenizer, item, e["reply"], limit))
    noctx = logps(bare)
    return [[lp / n, lp0 / n, (lp - lp0) / n, eom, math.log(n)]
            for (lp, n, eom), (lp0, _, _) in zip(ctx, noctx)]


JUDGE_CHUNK = 8  # replies scored per pass; bounds memory when a meow brings 32 candidates


def judge_scores(judge: ReplyJudge, tokenizer: Tokenizer, item: dict, replies: list[list[str]]) -> list[float]:
    """Expected grade (0..classes-1) of each reply as the bot's reply to the chat in item."""
    if len(replies) > JUDGE_CHUNK:
        return [g for i in range(0, len(replies), JUDGE_CHUNK)
                for g in judge_scores(judge, tokenizer, item, replies[i:i + JUDGE_CHUNK])]
    limit, pad = judge.lm.cfg.max_length, tokenizer.id["<pad>"]
    examples = []
    for reply in replies:
        ids, start, end = judge_encode(tokenizer, item, reply, limit)
        examples.append({"ids": ids, "start": start, "end": end, "item": item, "reply": reply})
    feats = fit_features(judge, tokenizer, examples, limit) if judge.n_feat else None
    ids = padded([e["ids"] for e in examples], pad)
    start = torch.tensor([e["start"] for e in examples])
    end = torch.tensor([e["end"] for e in examples])
    h = judge.lm.hidden_states(ids)
    rows = torch.arange(ids.shape[0])
    pos = torch.arange(ids.shape[1])[None]
    mask = ((pos >= start[:, None]) & (pos <= end[:, None])).to(h.dtype)[..., None]
    pooled = torch.cat((h[rows, end], (h * mask).sum(1) / mask.sum(1).clamp(min=1)), -1)
    if feats is not None:
        pooled = torch.cat((pooled, torch.tensor(feats).to(pooled.dtype)), -1)
    logits = judge.head(pooled.float())
    return (logits.softmax(-1) * torch.arange(logits.shape[-1])).sum(-1).tolist()


# ---- picker helpers (lm_watch) -----------------------------------------------------------------------------
def content_words(text):
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


def repeat_share(text, own_words):
    words = content_words(text)
    return sum(w in own_words for w in words) / len(words) if words else 0.0


def is_meow(text):
    return bool(MEOW_ONLY.match(text.strip()))


def copy_key(text):
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def split_photo_tokens(tokenizer: Tokenizer, tokens: list[str]) -> list[str]:
    """Text tokens of a generated reply through <eom>, without photo labels and <img> markers
    (lm_photo_output.split_generated_photo_tokens, text part)."""
    text, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("<eom>", "<eos>"):
            break
        i += 1
        if token == "<img>":
            continue
        if token != "<photo>":
            text.append(token)
            continue
        while i < len(tokens) and tokens[i].startswith(SP) and tokens[i][1:] in tokenizer.protected:
            i += 1
    return text


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Engine:
    """Loaded once; reply() is not thread-safe (the worker handles one request at a time)."""

    def __init__(self, bundle: Path, threads: int | None = None, verify: bool = True):
        bundle = Path(bundle)
        if threads:
            torch.set_num_threads(threads)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        if verify:
            for name, digest in manifest["sha256"].items():
                if sha256(bundle / name) != digest:
                    raise ValueError(f"{name} does not match the bundle manifest")
        self.manifest = manifest
        self.tokenizer = tok = Tokenizer.load(bundle / "tokenizer.json")
        pad = tok.id["<pad>"]
        gen = torch.load(bundle / "generator.pt", map_location="cpu", weights_only=True, mmap=True)
        if gen["config"]["vocab_size"] != len(tok.vocab):
            raise ValueError("tokenizer does not match the chat model")
        self.model = build_model(gen["config"], gen["untied"], gen["state_dict"], pad)
        self.step = gen.get("step")
        del gen
        saved = torch.load(bundle / "judge.pt", map_location="cpu", weights_only=True, mmap=True)
        lm = build_model(saved["config"], saved["untied"], saved["lm"], pad)
        ref = build_model(saved["config"], saved["ref_untied"], saved["ref"], pad) if saved["feats"] else None
        self.judge = ReplyJudge(lm, ref, saved["hidden"], saved["classes"], saved["feats"])
        self.judge.head.load_state_dict(saved["head"])
        self.judge.eval().requires_grad_(False)
        del saved
        blocklist = bundle / "blocklist"

        def private_list(name):
            path = blocklist / name
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        names = [re.escape(n).replace(r"\ ", r"[\s_.]+")
                 for n in sorted(private_list("member_full_names.json"), key=len, reverse=True)]
        self.blocked = [p for p in (word_pattern(names), word_pattern(private_list("slur_patterns.json"))) if p is not None]
        # Name stems the bot must never write, matched at the start of any word ("Name", "names").
        self.blocked += [re.compile(r"(?<![^\W_])" + stem, re.IGNORECASE)
                         for stem in private_list("name_stems.json")]
        banned = {tok.id[t] for t in NEVER if t in tok.id}
        banned |= {i for i, t in enumerate(tok.vocab) if "@" in t or "＠" in t}
        self.banned = sorted(banned)
        self.speaker_ids = [i for i, t in enumerate(tok.vocab) if is_speaker(t)]

    # ---- safety ----
    def unsendable(self, text: str) -> bool:
        """A command to the bot, anything that could ping, a blocked name or a slur."""
        return (is_bot_command(text) or bool(re.search(r"@|＠|<@|<#", text))
                or any(p.search(text) for p in self.blocked))

    # ---- sampling ----
    @torch.inference_mode()
    def sample(self, prefix: list[str], n: int, generator: torch.Generator, contrast: float = CONTRAST,
               temperature: float = TEMPERATURE, top_p: float = TOP_P) -> list[list[str] | None]:
        """n one-message replies to prefix (ending with the reply's speaker slot), sampled together."""
        tok, model = self.tokenizer, self.model
        prompts = [fit(list(prefix), model.cfg.max_length - MAX_TOKENS)]
        if contrast:
            prompts.append(bare_prefix(prefix))
        states = [PromptRows(model, [tok.id[t] for t in p], n) for p in prompts]
        blocked = torch.zeros(states[0].logits.shape[1], dtype=torch.bool)
        blocked[self.speaker_ids] = True
        blocked[[tok.id["<eos>"], tok.id["<re>"]] + self.banned] = True
        eom = tok.id["<eom>"]
        rows = [[] for _ in range(n)]
        done = torch.zeros(n, dtype=torch.bool)
        for _ in range(MAX_TOKENS):
            logps = [torch.log_softmax(st.logits.float().masked_fill(blocked, -torch.inf), dim=-1) for st in states]
            mixed = torch.logsumexp(torch.stack(logps[:1]), dim=0)
            if contrast:
                ok = mixed >= mixed.max(dim=-1, keepdim=True).values + math.log(PLAUSIBLE)
                score = ((1 + contrast) * mixed - contrast * logps[-1].clamp_min(-1e4)).masked_fill(~ok, -torch.inf)
            else:
                score = mixed
            probs = torch.softmax(score / temperature, dim=-1)
            ordered, order = probs.sort(dim=-1, descending=True)
            ordered = ordered * ((ordered.cumsum(-1) - ordered) < top_p)
            token = order.gather(1, torch.multinomial(ordered, 1, generator=generator))[:, 0]
            token = torch.where(done, torch.full_like(token, eom), token)
            for r, t in enumerate(token.tolist()):
                if not rows[r] or rows[r][-1] != eom:
                    rows[r].append(t)
            done |= token == eom
            if bool(done.all()):
                break
            for st in states:
                st.step(token)
        replies = []
        for row in rows:
            tokens = [tok.vocab[t] for t in row]
            if not tokens or tokens[-1] != "<eom>" or self.unsendable(tok.decode(tokens[:-1])):
                replies.append(None)
            else:
                replies.append(tokens)
        return replies

    # ---- picking ----
    def scored_candidates(self, prefix, item, messages, generator, n, echoes=None):
        """[(pick score, reply tokens, text)] for the usable candidates (lm_watch.score_candidates with
        MuonKahuna's settings; with a judge the per-token gain is not computed). Candidates that only copy a
        chat message are left out; if `echoes` is a list they are added to it (tokens) for the last resort."""
        tok = self.tokenizer
        blocked = {copy_key(m["text"]) for m in messages}
        usable, unique = [], set()
        # Sampled SAMPLE_CHUNK rows at a time: the key/value caches grow with the rows, and a meow's 32 rows at
        # once cost ~300 MB for a moment; same distribution, one chunk per pass.
        sampled = [r for i in range(0, n, SAMPLE_CHUNK)
                   for r in self.sample(prefix, min(SAMPLE_CHUNK, n - i), generator)]
        for reply in sampled:
            if not reply or len(reply) < 2 or tuple(reply) in unique:
                continue
            unique.add(tuple(reply))
            text = tok.decode(split_photo_tokens(tok, reply)).strip().lower()
            if not text:
                continue
            if copy_key(text) in blocked:
                if echoes is not None and reply not in echoes:
                    echoes.append(reply)
                continue
            usable.append(reply)
        return self.score(usable, item, messages)

    def score(self, replies, item, messages):
        """Pick scores for reply token lists: judge grade plus the dashboard's picker bonuses and penalties."""
        tok = self.tokenizer
        own_words = {w for m in messages if m["s"] == BOT_SPEAKER for w in content_words(m["text"])}
        recent_words = {w for m in messages[-3:] for w in content_words(m["text"])}
        chat_cats = ({t for m in messages for t in tok.tokens(m["text"]) if t in tok.protected}
                     | {c for m in messages for c in (m.get("photo") or [])})
        meow_in = is_meow(messages[-1]["text"])
        scored, judged = [], []
        for reply in replies:
            text_tokens = split_photo_tokens(tok, reply)
            text = tok.decode(text_tokens).strip().lower()
            bonus = LENGTH_BONUS * math.log(max(1, len(set(text.split()))))
            if meow_in and is_meow(text):
                bonus += MEOW_BONUS
            if not is_meow(text):
                words = content_words(text)
                if len(words) > 1:
                    bonus -= SELF_REPEAT_PENALTY * (1 - len(set(words)) / len(words))
            if any(t in tok.protected and t not in chat_cats for t in text_tokens):
                bonus -= NEW_CAT_PENALTY
            base = (-REPEAT_PENALTY * repeat_share(text, own_words)
                    - ECHO_PENALTY * repeat_share(text, recent_words) + bonus)
            scored.append([base, reply, text])
            if text_tokens[:1] == ["<re>"] and len(text_tokens) > 1 and is_speaker(text_tokens[1]):
                text_tokens = text_tokens[2:]
            judged.append(text_tokens + ["<eom>"])
        if scored:
            with torch.inference_mode():
                grades = judge_scores(self.judge, tok, item, judged)
            for row, grade in zip(scored, grades):
                row[0] += grade / JUDGE_SPREAD
        return scored

    def reply(self, messages: list[dict], seed: int | None = None, year: int | None = None) -> dict:
        """messages: [{"s": speaker number (2 = the bot), "text": str, "img": bool?}], oldest first, the last one
        from a person. Returns {"text": reply for Discord, "candidates": n usable, "step": model step}."""
        if not messages or len(messages) > MAX_MESSAGES:
            raise ValueError("Send one to twenty messages.")
        msgs = []
        for m in messages:
            s, text = int(m["s"]), str(m.get("text") or "")[:MAX_CHARS]
            if not 1 <= s <= 8:
                raise ValueError("Speaker numbers run from 1 to 8.")
            item = {"s": s, "text": text}
            if m.get("img"):
                item["img"] = True
            msgs.append(item)
        if msgs[-1]["s"] == BOT_SPEAKER:
            raise ValueError("The last message must be from a person.")
        tok = self.tokenizer
        year = year or datetime.now().year
        session = {"year": year, "channel": CHANNEL, "msgs": msgs}
        prefix = tok.session_tokens(session, tags=True)[:-1] + [f"<s{BOT_SPEAKER}>"]
        item = {"year": year, "channel": CHANNEL, "context": msgs, "speaker": BOT_SPEAKER, "re": msgs[-1]["s"]}
        seed = random.SystemRandom().randrange(1 << 63) if seed is None else seed
        generator = torch.Generator().manual_seed(seed)
        n = MEOW_CANDIDATES if is_meow(msgs[-1]["text"]) else CANDIDATES
        echoes = []  # candidates that only copied a chat message ("WHAT?" -> "what????")
        scored = self.scored_candidates(prefix, item, msgs, generator, n, echoes)
        if not scored:
            scored = self.scored_candidates(prefix, item, msgs, generator, FALLBACK_CANDIDATES, echoes)
        blocked = {copy_key(m["text"]) for m in msgs}
        tries = 0
        while not scored:  # last resort: plain samples at 0.8 / 0.95, still no copies of the chat
            if tries == 5:
                if not echoes:
                    raise ValueError("The model only repeated the chat.")
                # Every candidate was an echo: send the best-scoring one rather than nothing.
                scored = self.score(echoes, item, msgs)
                break
            tries += 1
            for reply in self.sample(prefix, 1, generator, contrast=0.0, temperature=0.8, top_p=0.95):
                if reply:
                    text_tokens = split_photo_tokens(tok, reply)
                    if text_tokens[:1] == ["<re>"] and len(text_tokens) > 1 and is_speaker(text_tokens[1]):
                        text_tokens = text_tokens[2:]
                    text = tok.decode(text_tokens).strip()
                    if not text:
                        continue
                    if copy_key(text) not in blocked:
                        scored = [[0.0, reply, text]]
                    elif len(reply) >= 2 and reply not in echoes:
                        echoes.append(reply)
        best = max(scored, key=lambda s: s[0])
        text_tokens = split_photo_tokens(tok, best[1])
        if text_tokens[:1] == ["<re>"] and len(text_tokens) > 1 and is_speaker(text_tokens[1]):
            text_tokens = text_tokens[2:]
        return {"text": tok.decode(text_tokens, discord=True).strip(), "plain": tok.decode(text_tokens).strip(),
                "candidates": len(scored), "score": round(best[0], 3), "step": self.step, "seed": seed}
