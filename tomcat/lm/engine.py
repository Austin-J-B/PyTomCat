"""TomCatGPT inference on CPU: the dashboard's "Nerman int8" chat (or, from an older bundle, MuonKahuna), best of 4.

A runtime copy of the research path (scripts/lm_watch.chat_reply with lm_sample's steered sampler, and either
lm_nerman_picker's one-model picker or lm_reply_judge's reviewer), cut down to what one model needs and free of
the research imports. The weights come from a bundle folder written by scripts/export_tomcatgpt_bundle.py.

A Nerman bundle (it has picker.pt):
    generator.pt   the chat model (nerman_bo16), int8 except    {"config", "untied", "state_dict", "int8",
                   the feed-forward down layers ("mixed")         "int8_mode", "step"}
    picker.pt      the head on the chat model's own layers      {"dim", "hidden", "state_dict", "layers",
                   (lm_nerman_picker, distilled from the 1.7B    "flags", "flag_cutoffs", "score_sd",
                   judge)                                        "max_tokens"}
A MuonKahuna bundle (kept for rollback) has judge.pt instead:
    generator.pt   the chat model (muon_u1_bo16n)             {"config", "untied", "state_dict", "step"}
    judge.pt       the reviewer (judge_mk_v3) and its frozen   {"config", "untied", "lm", "head", "hidden",
                   base model for the fit features              "classes", "feats", "ref", "ref_untied"}
Both:
    tokenizer.json the vocabulary the models were trained with
    blocklist/     member_full_names.json, slur_patterns.json, name_stems.json (private; never in git)
    person_names.json  lowercase first names / handles of people (private; optional): names the bot already wrote in
                   a thread are made less likely (NAME_REPEAT_PENALTY)
    manifest.json  sha256 of each file, checked at load

How a reply is picked (MuonKahuna's CHAT_SETTINGS on the dashboard, 2026-11-24): 4 candidates sampled
together with context-aware decoding (contrast 1.0, T 0.6, top-p 0.9, max 80 tokens), copies of any chat
message dropped, then the best by
    judge / 0.38 + 0.5 ln(distinct words) - 2 x share of words the bot already said
    - 0.5 x echo of the last 3 messages - 3 x self-repeat share - 3 if it names a cat the chat didn't.
A message that is only cat sounds gets 32 candidates and a bonus for meowing back.

Nerman (dashboard "Nerman int8", 2026-11-28) picks the same way with the picker's score in its own SDs in place of
judge / 0.38; a try the picker flags (contradicting, repeating, nonsense) or that copies 70%+ of a chat message
only wins when every try is flagged. It writes as in its server training: channel "other", answering the last
message with the reply button. When every try only copied a cat sound, the reply starts with that sound and the
model finishes it its own way ("meow :3").

This module imports torch: only the worker process (tomcat/lm/worker.py) may import it, never the bot.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import warnings
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
SAMPLE_CHUNK = 8  # rows sampled per pass at least (more when they fit KV_CACHE_BUDGET; see cache_rows)
KV_CACHE_BUDGET = 192 << 20  # bytes of key/value cache per pass for the prompt with the chat
# A person's name the bot already wrote in this thread is made e^-NAME_REPEAT_PENALTY times as likely at every
# step (AJ 2026-11-24: names are fine, the bot just gets stuck on one; control it, don't block it). 0 turns it off.
# Measured on the 16 #bot-spam threads (2026-11-24) where the bot had named someone, best of 4, 8 seeds each:
# the pick repeats the name 59% of the time at 0, 38% at 3, 24% at 4, 4% at 5; the reviewer's grade of the pick
# does not drop (2.12 / 2.15 / 2.18 / 2.20) and fewer turns end with no usable candidate (34 / 22 / 14 / 13 of 128).
NAME_REPEAT_PENALTY = 4.0
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
# Nerman's vocabulary has name pointers ("<name_2>": speaker 2's name): the bot has no names to put there, so it
# never writes one.
NAME_POINTER = re.compile(r"<name_(?:[1-8]|other)>")

# ---- Nerman picker (lm_nerman_picker) ----------------------------------------------------------------------
NERMAN_CHANNEL = "other"  # server chat was trained with every channel as "other" (nerman/build_server.py)
FLAG_PENALTY = 100.0       # a flagged try only wins when every try is flagged
FEATURE_CHUNK = 4          # replies per feature pass (bounds memory when a meow brings 32 candidates)
MEOW_VARIATION_TRIES, MEOW_VARIATION_TOKENS = 16, 40


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
    loops: int = 1
    loop_max: int = 0
    # Nerman options (all off by default, so MuonKahuna loads and runs unchanged): grouped-query attention
    # (kv_heads), separate q/k/v, QK-norm, logit soft-cap, value residual, U-net skips, per-head output gate,
    # one-gain norms, and projections after the embedding and before the output scores.
    kv_heads: int = 0
    split_qkv: bool = False
    qk_norm: bool = False
    logit_softcap: float = 0.0
    value_residual: bool = False
    unet_skips: bool = False
    head_gate: bool = False
    single_scale_norm: bool = False
    embed_proj: bool = False


class RMSNorm(nn.Module):
    def __init__(self, width, eps=1e-6, single_scale=False):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(1 if single_scale else width))
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
    def __init__(self, cfg, index=0):
        super().__init__()
        self.heads = cfg.heads
        self.modern = cfg.variant == "modern"
        single = cfg.single_scale_norm
        self.norm1 = RMSNorm(cfg.width, single_scale=single) if self.modern else nn.LayerNorm(cfg.width)
        self.norm2 = RMSNorm(cfg.width, single_scale=single) if self.modern else nn.LayerNorm(cfg.width)
        self.kv_heads = cfg.kv_heads or cfg.heads
        head = cfg.width // cfg.heads
        self.split = cfg.split_qkv or self.kv_heads != cfg.heads
        if self.split:
            self.q = nn.Linear(cfg.width, cfg.width, bias=not self.modern)
            self.k = nn.Linear(cfg.width, self.kv_heads * head, bias=not self.modern)
            self.v = nn.Linear(cfg.width, self.kv_heads * head, bias=not self.modern)
        else:
            self.qkv = nn.Linear(cfg.width, 3 * cfg.width, bias=not self.modern)
        self.qk_norm = cfg.qk_norm
        if self.qk_norm:
            self.q_norm, self.k_norm = RMSNorm(head), RMSNorm(head)
        self.proj = nn.Linear(cfg.width, cfg.width, bias=not self.modern)
        if self.modern:
            gated = max(8, round((cfg.feedforward * 2 / 3) / 8) * 8)
            self.gate = nn.Linear(cfg.width, gated, bias=False)
            self.up = nn.Linear(cfg.width, gated, bias=False)
            self.down = nn.Linear(gated, cfg.width, bias=False)
        else:
            self.up, self.down = nn.Linear(cfg.width, cfg.feedforward), nn.Linear(cfg.feedforward, cfg.width)
        self.head_gate = nn.Linear(cfg.width, cfg.heads, bias=False) if cfg.head_gate else None
        # [own values, first block's values]; the first block has nothing to mix in.
        self.v_mix = nn.Parameter(torch.tensor([.5, .5])) if cfg.value_residual and index > 0 else None

    def project(self, h):
        """Queries [b, heads, n, hd] and keys, values [b, kv_heads, n, hd] for the normed input h (QK-norm, no RoPE)."""
        b, n, w = h.shape
        hd = w // self.heads
        if self.split:
            q = self.q(h).view(b, n, self.heads, hd)
            k, v = self.k(h).view(b, n, self.kv_heads, hd), self.v(h).view(b, n, self.kv_heads, hd)
        else:
            q, k, v = self.qkv(h).view(b, n, 3, self.heads, hd).unbind(2)
        if self.qk_norm:
            q, k = self.q_norm(q), self.k_norm(k)
        return q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)

    def expand_kv(self, t):
        """[b, kv_heads, n, hd] -> [b, heads, n, hd]: key/value head j serves query heads j*g .. j*g+g-1."""
        return t if self.kv_heads == self.heads else t.repeat_interleave(self.heads // self.kv_heads, dim=1)

    def mix_values(self, v, first_v):
        if self.v_mix is None or first_v is None:
            return v
        return self.v_mix[0] * v + self.v_mix[1] * first_v

    def attend_out(self, a, h):
        """Attention output a [b, heads, n, hd] -> the update added to the stream (head gate, then projection)."""
        if self.head_gate is not None:
            a = a * torch.sigmoid(self.head_gate(h)).transpose(1, 2)[..., None]
        b, _, n, _ = a.shape
        return self.proj(a.transpose(1, 2).reshape(b, n, -1))

    def feed_forward(self, x):
        hidden = self.norm2(x)
        hidden = F.silu(self.gate(hidden)) * self.up(hidden) if self.modern else F.gelu(self.up(hidden))
        return self.down(hidden)

    def step(self, x, attend, first_v=None):
        """One block with the attention left to attend(q, k, v) -> a [b, heads, n, hd] (q, k QK-normed, no RoPE;
        v mixed). Returns (x, this block's own values)."""
        h = self.norm1(x)
        q, k, v = self.project(h)
        a = attend(q, k, self.mix_values(v, first_v))
        x = x + self.attend_out(a, h)
        return x + self.feed_forward(x), v

    def causal_attention(self, q, k, v, factors):
        if self.modern:
            q, k = rotary(q, factors), rotary(k, factors)
        return F.scaled_dot_product_attention(q, self.expand_kv(k), self.expand_kv(v), is_causal=True)


class CausalLM(nn.Module):
    def __init__(self, cfg, pad, untied=False):
        super().__init__()
        if cfg.loops != 1 or cfg.loop_max:
            raise ValueError("looped models are not supported")
        self.cfg = cfg
        self.token = nn.Embedding(cfg.vocab_size, cfg.width, padding_idx=pad)
        self.position = nn.Embedding(cfg.max_length, cfg.width) if cfg.variant == "classic" else None
        if cfg.variant == "modern":
            cosine, sine = rotary_factors(cfg.max_length, cfg.width // cfg.heads)
            self.register_buffer("rope_cosine", cosine, persistent=False)
            self.register_buffer("rope_sine", sine, persistent=False)
        self.blocks = nn.ModuleList(CausalBlock(cfg, i) for i in range(cfg.layers))
        self.norm = (RMSNorm(cfg.width, single_scale=cfg.single_scale_norm) if cfg.variant == "modern"
                     else nn.LayerNorm(cfg.width))
        self.bias = nn.Parameter(torch.zeros(cfg.vocab_size))
        self.output = nn.Linear(cfg.width, cfg.vocab_size, bias=False) if untied else None
        self.skip_weights = nn.Parameter(torch.ones(cfg.layers // 2)) if cfg.unet_skips else None
        self.embed_in = nn.Linear(cfg.width, cfg.width, bias=False) if cfg.embed_proj else None
        self.embed_out = nn.Linear(cfg.width, cfg.width, bias=False) if cfg.embed_proj else None

    @property
    def out_weight(self):
        return self.output.weight if self.output is not None else self.token.weight

    def factors(self, start, steps):
        if self.cfg.variant != "modern":
            return None
        return self.rope_cosine[start:start + steps], self.rope_sine[start:start + steps]

    def embed(self, ids, start=0):
        x = self.token(ids)
        if self.embed_in is not None:
            x = self.embed_in(x)
        if self.position is not None:
            x = x + self.position(torch.arange(start, start + ids.shape[1]))[None]
        return x

    def head(self, x):
        """Final norm, output projection when set, output scores, and the logit soft-cap when set."""
        h = self.norm(x)
        if self.embed_out is not None:
            h = self.embed_out(h)
        logits = F.linear(h, self.out_weight, self.bias)
        cap = self.cfg.logit_softcap
        return cap * torch.tanh(logits / cap) if cap else logits

    def walk(self, x, attend, each=None):
        """The block stack with the attention left to attend(layer, block, q, k, v), applying the value residual
        and U-net skips (pretrain_ccc_causal.CausalLM.walk). each(layer, x), when given, sees every block's output."""
        skips, first_v = [], None
        layers = len(self.blocks)
        half = layers // 2 if self.skip_weights is not None else 0
        for layer, block in enumerate(self.blocks):
            if half and layer >= layers - half:
                x = x + self.skip_weights[layer - (layers - half)] * skips.pop()
            x, v = block.step(x, lambda q, k, v, layer=layer, block=block: attend(layer, block, q, k, v), first_v)
            if self.cfg.value_residual and first_v is None:
                first_v = v
            if layer < half:
                skips.append(x)
            if each is not None:
                each(layer, x)
        return x

    def causal(self, ids, each=None):
        """Block-stack output for ids [rows, n] from position 0 (plain causal attention)."""
        factors = self.factors(0, ids.shape[1])
        return self.walk(self.embed(ids), lambda layer, block, q, k, v: block.causal_attention(q, k, v, factors),
                         each)

    def hidden_states(self, ids):
        return self.norm(self.causal(ids))


def quantized_linear(entry: dict) -> nn.Module:
    """A dynamic int8 Linear (per-channel weights, CPU) from the exported int8 values, scales and zero points:
    the same packed weights torch.ao.quantization.quantize_dynamic made in research (lm_nerman_quant.py).
    torch 2.15 marks quantized tensors deprecated: where a later torch has dropped them, the layer is the same
    weights expanded to fp32 (a plain Linear; ~3x the memory for these layers, same replies within noise)."""
    values, bias = entry["int8"], entry.get("bias")
    try:
        from torch.ao.nn.quantized.dynamic import Linear as DynamicLinear
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*quantized tensor creation functions.*")
            weight = torch._make_per_channel_quantized_tensor(values, entry["scales"], entry["zero_points"], 0)
            layer = DynamicLinear(values.shape[1], values.shape[0], bias_=bias is not None, dtype=torch.qint8)
            layer.set_weight_bias(weight, bias)
        return layer
    except (ImportError, AttributeError, RuntimeError, NotImplementedError):
        layer = nn.Linear(values.shape[1], values.shape[0], bias=bias is not None)
        scale, zero = entry["scales"].float()[:, None], entry["zero_points"].float()[:, None]
        layer.weight = nn.Parameter((values.float() - zero) * scale, requires_grad=False)
        if bias is not None:
            layer.bias = nn.Parameter(bias.float(), requires_grad=False)
        return layer


def build_model(config: dict, untied: bool, state: dict, pad: int, int8: dict | None = None) -> CausalLM:
    """assign=True uses the loaded tensors as the weights instead of copying them in: with torch.load(mmap=True)
    they stay file-backed pages, which the kernel can drop and re-read, rather than a second private copy.
    The layers skip their random initialisation: the placeholder weights stay untouched (never resident) until the
    loaded ones replace them, instead of ~216 MB per model written for a moment at load. (The meta device would do
    the same but its first use costs ~70 MB of memory for good.)
    int8: {module name: quantized_linear entry} for the layers stored as int8; their packed weights live in this
    process's own memory (the int8 values, not fp32)."""
    linear, embedding = nn.Linear.reset_parameters, nn.Embedding.reset_parameters
    nn.Linear.reset_parameters = nn.Embedding.reset_parameters = lambda self: None
    try:
        model = CausalLM(CausalConfig(**config), pad, untied)
    finally:
        nn.Linear.reset_parameters, nn.Embedding.reset_parameters = linear, embedding
    int8 = int8 or {}
    missing, unexpected = model.load_state_dict(state, assign=True, strict=False)
    missing = [k for k in missing if k.rpartition(".")[0] not in int8]
    if missing or unexpected:
        raise ValueError(f"chat model weights do not match its config: missing {missing[:3]}, extra {unexpected[:3]}")
    for name, entry in int8.items():  # replaces the never-filled fp32 placeholder
        parent, _, leaf = name.rpartition(".")
        setattr(model.get_submodule(parent), leaf, quantized_linear(entry))
    return model.eval().requires_grad_(False)


# ---- sampling (lm_sample.PromptRows / generate_replies_steered) --------------------------------------------
class PromptRows:
    """n sampling rows sharing one prompt: the prompt runs once, its keys/values are copied to every row, then
    step() adds one token per row. .logits holds the latest next-token logits [n, vocab]."""

    def __init__(self, model: CausalLM, ids: list[int], n: int, new_tokens: int = MAX_TOKENS):
        cfg, block0 = model.cfg, model.blocks[0]
        self.model = model
        length = min(cfg.max_length, len(ids) + new_tokens)  # only these positions are ever written
        self.k = torch.zeros((len(model.blocks), n, block0.kv_heads, length, cfg.width // cfg.heads))
        self.v = torch.zeros_like(self.k)
        self.logits = self._run(torch.tensor([ids]), 0).expand(n, -1)
        self.k[:, 1:] = self.k[:, :1]
        self.v[:, 1:] = self.v[:, :1]
        self.position = len(ids)

    def step(self, token):
        self.logits = self._run(token[:, None], self.position)
        self.position += 1
        return self.logits

    def keep_rows(self, index):
        """Keep only these rows (finished rows leave the batch)."""
        self.k, self.v, self.logits = self.k[:, index], self.v[:, index], self.logits[index]

    def attend(self, layer, block, q, k, v, start):
        """Attention for rows q, k, v [rows, heads, steps, hd] at positions start..: writes this prompt's cache."""
        rows, steps = q.shape[0], q.shape[2]
        if block.modern:
            factors = self.model.factors(start, steps)
            q, k = rotary(q, factors), rotary(k, factors)
        self.k[layer, :rows, :, start:start + steps] = k
        self.v[layer, :rows, :, start:start + steps] = v
        keys = block.expand_kv(self.k[layer, :rows, :, :start + steps])
        values = block.expand_kv(self.v[layer, :rows, :, :start + steps])
        return F.scaled_dot_product_attention(q, keys, values, is_causal=steps > 1 and start == 0)

    def _run(self, ids, start):
        model = self.model
        x = model.walk(model.embed(ids, start),
                       lambda layer, block, q, k, v: self.attend(layer, block, q, k, v, start))
        return model.head(x[:, -1])


def cache_rows(model: CausalLM, prompt_tokens: int, new_tokens: int = MAX_TOKENS) -> int:
    """Rows sampled per pass: as many as fit KV_CACHE_BUDGET for this prompt length, never fewer than
    SAMPLE_CHUNK. Each token step costs about the same for 1 or 32 rows (the time goes to reading the weights), so
    fewer passes are faster; the cache memory is what grows with the rows."""
    cfg, block0 = model.cfg, model.blocks[0]
    length = min(cfg.max_length, prompt_tokens + new_tokens)
    per_row = len(model.blocks) * block0.kv_heads * length * (cfg.width // cfg.heads) * 4 * 2
    return max(SAMPLE_CHUNK, KV_CACHE_BUDGET // per_row)


def step_together(states: list[PromptRows], token: torch.Tensor) -> None:
    """Add the same next token to every state in one pass through the weights (the contrast decoding's prompt
    with the chat and the one without): the rows are stacked for every layer, and only attention runs per state
    on its own cache and positions. Sets each state's .logits."""
    model, n = states[0].model, token.shape[0]
    if len(states) == 1 or model.position is not None:  # learned positions differ per state: one pass each
        for st in states:
            st.step(token)
        return
    starts = [st.position for st in states]

    def attend(layer, block, q, k, v):
        return torch.cat([st.attend(layer, block, q[i * n:(i + 1) * n], k[i * n:(i + 1) * n],
                                    v[i * n:(i + 1) * n], start) for i, (st, start) in enumerate(zip(states, starts))])

    x = model.walk(model.embed(token[:, None].repeat(len(states), 1)), attend)
    logits = model.head(x[:, -1])
    for i, st in enumerate(states):
        st.logits = logits[i * n:(i + 1) * n]
        st.position += 1


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


# ---- Nerman's picker (lm_nerman_picker: a head on the chat model's own layers) ----------------------------
class PickerHead(nn.Module):
    def __init__(self, dim, hidden, flags):
        super().__init__()
        self.register_buffer("mu", torch.zeros(dim))
        self.register_buffer("sd", torch.ones(dim))
        self.net = nn.Sequential(nn.Linear(dim, hidden), nn.GELU(), nn.Dropout(0.1),
                                 nn.Linear(hidden, hidden), nn.GELU(), nn.Dropout(0.1))
        self.out = nn.Linear(hidden, 1 + flags)

    def forward(self, x):
        y = self.out(self.net((x - self.mu) / self.sd))
        return y[:, 0], torch.sigmoid(y[:, 1:])


def bare_of(prefix: list[str]) -> list[str]:
    """Header + the reply opener, no chat (lm_nerman_picker.bare_of)."""
    first = next(i for i, t in enumerate(prefix) if is_speaker(t))
    tail = prefix[-3:] if len(prefix) >= 3 and prefix[-2] == "<re>" else prefix[-1:]
    return prefix[:first] + tail


def overlap(a, b):
    x, y = set(a.lower().split()), set(b.lower().split())
    return len(x & y) / max(1, len(x))


def copies_chat(reply, chat_texts):
    """4+ words and 70%+ of them in one chat message (lm_judge17_repick.copies_chat)."""
    return len(reply.split()) >= 4 and any(overlap(reply, t) >= 0.7 for t in chat_texts)


def reply_pass(model: CausalLM, prefix_ids: list[int], replies: list[list[int]], pad: int, layers=()):
    """({layer: states at the reply tokens}, log p of each reply token) for prefix + each reply, right padded
    (lm_nerman_picker._pass). Padding follows every real token, so the causal mask keeps it out of real positions."""
    p = len(prefix_ids)
    ids = torch.full((len(replies), p + max(map(len, replies))), pad, dtype=torch.long)
    for i, r in enumerate(replies):
        ids[i, :p] = torch.tensor(prefix_ids)
        ids[i, p:p + len(r)] = torch.tensor(r)
    keep = {}
    x = model.causal(ids, each=lambda layer, h: keep.__setitem__(layer, h[:, p:]) if layer in layers else None)
    logits = model.head(x[:, p - 1:-1]).float()
    return keep, torch.log_softmax(logits, -1).gather(-1, ids[:, p:, None]).squeeze(-1)


def picker_features(model: CausalLM, tokenizer: Tokenizer, prefix: list[str], replies: list[list[str]],
                    layers) -> torch.Tensor:
    """[replies, 4 x width + 4]: per layer the mean reply-token state and the last (<eom>) token's state, then
    nats/token with the chat, without it, their difference, ln(tokens) (lm_nerman_picker.features)."""
    tok = tokenizer
    pad, unk = tok.id["<pad>"], tok.id["<unk>"]
    pre = [tok.id.get(t, unk) for t in prefix]
    bare = [tok.id.get(t, unk) for t in bare_of(prefix)]
    out = []
    for k in range(0, len(replies), FEATURE_CHUNK):
        chunk = [[tok.id.get(t, unk) for t in r] for r in replies[k:k + FEATURE_CHUNK]]
        hid, lp = reply_pass(model, pre, chunk, pad, layers)
        _, lpb = reply_pass(model, bare, chunk, pad)
        lens = torch.tensor([len(r) for r in chunk])
        m = (torch.arange(lp.shape[1])[None] < lens[:, None]).float()
        nll = -(lp * m).sum(1) / lens
        nllb = -(lpb * m).sum(1) / lens
        parts = []
        for layer in layers:
            h = hid[layer].float()
            parts.append((h * m[..., None]).sum(1) / lens[:, None])
            parts.append(h[torch.arange(len(chunk)), lens - 1])
        parts.append(torch.stack([nll, nllb, nllb - nll, lens.float().log()], 1))
        out.append(torch.cat(parts, 1))
    return torch.cat(out)


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
        self.model = build_model(gen["config"], gen["untied"], gen["state_dict"], pad, gen.get("int8"))
        self.step = gen.get("step")
        del gen
        self.judge = self.picker = None
        if (bundle / "picker.pt").is_file():  # Nerman: the picker reads the chat model's own layers
            saved = torch.load(bundle / "picker.pt", map_location="cpu", weights_only=True)
            self.picker = PickerHead(saved["dim"], saved["hidden"], len(saved["flags"]))
            self.picker.load_state_dict(saved["state_dict"])
            self.picker.eval().requires_grad_(False)
            self.picker_layers = tuple(saved["layers"])
            self.flag_cutoffs = torch.tensor(saved["flag_cutoffs"])
            self.score_sd = float(saved["score_sd"])
            self.picker_tokens = int(saved["max_tokens"])
            del saved
        else:
            saved = torch.load(bundle / "judge.pt", map_location="cpu", weights_only=True, mmap=True)
            lm = build_model(saved["config"], saved["untied"], saved["lm"], pad)
            ref = build_model(saved["config"], saved["ref_untied"], saved["ref"], pad) if saved["feats"] else None
            self.judge = ReplyJudge(lm, ref, saved["hidden"], saved["classes"], saved["feats"])
            self.judge.head.load_state_dict(saved["head"])
            self.judge.eval().requires_grad_(False)
            del saved
        self.channel = NERMAN_CHANNEL if self.picker is not None else CHANNEL
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
        banned |= {i for i, t in enumerate(tok.vocab) if "@" in t or "＠" in t or NAME_POINTER.fullmatch(t)}
        self.banned = sorted(banned)
        self.speaker_ids = [i for i, t in enumerate(tok.vocab) if is_speaker(t)]
        names_path = bundle / "person_names.json"
        names = json.loads(names_path.read_text(encoding="utf-8")) if names_path.is_file() else []
        # Only names that are one whole token (either spelling: message start / after a space), so the penalty
        # never touches a word piece another word shares.
        self.name_tokens = {}
        for name in names:
            pieces = [tok.tokens(form) for form in (name, " " + name)]
            ids = [tok.id[p[0]] for p in pieces if len(p) == 1]
            if ids:
                self.name_tokens[name] = ids

    def repeat_name_ids(self, messages: list[dict]) -> list[int]:
        """Token ids of the people's names the bot already wrote in this thread."""
        said = {w for m in messages if m["s"] == BOT_SPEAKER for w in re.findall(r"[a-z0-9_']+", m["text"].lower())}
        return sorted({i for w in said & self.name_tokens.keys() for i in self.name_tokens[w]})

    # ---- safety ----
    def unsendable(self, text: str) -> bool:
        """A command to the bot, anything that could ping, a blocked name or a slur."""
        return (is_bot_command(text) or bool(re.search(r"@|＠|<@|<#", text))
                or any(p.search(text) for p in self.blocked))

    # ---- sampling ----
    @torch.inference_mode()
    def sample(self, prefix: list[str], n: int, generator: torch.Generator, contrast: float = CONTRAST,
               temperature: float = TEMPERATURE, top_p: float = TOP_P,
               penalize: list[int] | None = None, max_tokens: int = MAX_TOKENS) -> list[list[str] | None]:
        """n one-message replies to prefix (ending where the reply's text starts), sampled together."""
        tok, model = self.tokenizer, self.model
        prompts = [fit(list(prefix), model.cfg.max_length - max_tokens)]
        if contrast:
            prompts.append(bare_prefix(prefix))
        states = [PromptRows(model, [tok.id[t] for t in p], n, max_tokens) for p in prompts]
        blocked = torch.zeros(states[0].logits.shape[1], dtype=torch.bool)
        blocked[self.speaker_ids] = True
        blocked[[tok.id["<eos>"], tok.id["<re>"]] + self.banned] = True
        shift = torch.zeros(blocked.shape[0])
        if penalize and NAME_REPEAT_PENALTY:
            shift[penalize] = -NAME_REPEAT_PENALTY
        eom = tok.id["<eom>"]
        rows = [[] for _ in range(n)]
        batch = list(range(n))  # batch row j is rows[batch[j]]
        done = torch.zeros(n, dtype=torch.bool)
        for _ in range(max_tokens):
            logps = [torch.log_softmax((st.logits.float() + shift).masked_fill(blocked, -torch.inf), dim=-1)
                     for st in states]
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
            for j, t in enumerate(token.tolist()):
                row = rows[batch[j]]
                if not row or row[-1] != eom:
                    row.append(t)
            done |= token == eom
            if bool(done.all()):
                break
            # Once half the batch has finished, drop the finished rows: past ~8 rows each step costs more per row,
            # and one long try would otherwise carry every finished row to the end (a meow's 32 tries).
            if 2 * int(done.sum()) >= len(batch):
                keep = (~done).nonzero()[:, 0]
                for st in states:
                    st.keep_rows(keep)
                batch, done, token = [batch[j] for j in keep.tolist()], done[keep], token[keep]
            step_together(states, token)
        replies = []
        for row in rows:
            tokens = [tok.vocab[t] for t in row]
            if not tokens or tokens[-1] != "<eom>" or self.unsendable(tok.decode(tokens[:-1])):
                replies.append(None)
            else:
                replies.append(tokens)
        return replies

    # ---- picking ----
    def scored_candidates(self, prefix, item, messages, generator, n, echoes=None, penalize=None):
        """[(pick score, reply tokens, text)] for the usable candidates (lm_watch.score_candidates with
        the dashboard's settings; with a judge or picker the per-token gain is not computed). Candidates that only
        copy a chat message are left out; if `echoes` is a list they are added to it (tokens) for the last resort."""
        tok = self.tokenizer
        blocked = {copy_key(m["text"]) for m in messages}
        usable, unique = [], set()
        # Sampled in chunks (cache_rows): the key/value caches grow with the rows; same distribution per chunk.
        chunk = cache_rows(self.model, len(prefix))
        sampled = [r for i in range(0, n, chunk)
                   for r in self.sample(prefix, min(chunk, n - i), generator, penalize=penalize)]
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
        return self.score(usable, item, messages, prefix)

    def score(self, replies, item, messages, prefix):
        """Pick scores for reply token lists: judge grade (or Nerman's picker score) plus the dashboard's picker
        bonuses and penalties."""
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
                if self.picker is not None:
                    grades = self.picker_scores(prefix, judged, [m["text"] for m in messages])
                else:
                    grades = [g / JUDGE_SPREAD for g in judge_scores(self.judge, tok, item, judged)]
            for row, grade in zip(scored, grades):
                row[0] += grade
        return scored

    def picker_scores(self, prefix, replies, chat_texts):
        """Nerman's picker score of each reply (text tokens + <eom>) in its own SDs, minus FLAG_PENALTY for a try
        it flags or that copies 70%+ of a chat message (lm_nerman_picker.live_judge, net of lm_watch's / JUDGE_SD)."""
        x = picker_features(self.model, self.tokenizer, prefix, [r[:self.picker_tokens] for r in replies],
                            self.picker_layers)
        score, flags = self.picker(x)
        flagged = (flags >= self.flag_cutoffs).any(1).tolist()
        out = []
        for s, f, reply in zip(score.tolist(), flagged, replies):
            text = self.tokenizer.decode([t for t in reply if t != "<eom>"])
            out.append(s / self.score_sd - (FLAG_PENALTY if f or copies_chat(text, chat_texts) else 0.0))
        return out

    def meow_variation(self, prefix, sound, blocked, generator, penalize=None):
        """When every try only copied a member's cat sound: start the reply with that sound and let the model
        finish it its own way ("meow :3", "meoww"), preferring a finish that is still only cat sounds
        (lm_watch.meow_variation, AJ 2026-11-28). Reply tokens ending in <eom>, or None."""
        tok = self.tokenizer
        start = tok.tokens(copy_key(sound))
        if not start:
            return None
        fresh = []
        for r in self.sample(list(prefix) + start, MEOW_VARIATION_TRIES, generator, contrast=0.0, temperature=0.8,
                             top_p=0.95, penalize=penalize, max_tokens=MEOW_VARIATION_TOKENS):
            if not r:
                continue
            text = tok.decode(start + r[:-1]).strip()
            if copy_key(text) not in blocked and not self.unsendable(text):
                fresh.append((not is_meow(text), start + r))
        return min(fresh, key=lambda f: f[0])[1] if fresh else None

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
        session = {"year": year, "channel": self.channel, "msgs": msgs}
        prefix = tok.session_tokens(session, tags=True)[:-1] + [f"<s{BOT_SPEAKER}>"]
        if self.picker is not None:  # Nerman answers with Discord's reply button, as every moment it trained on
            prefix += ["<re>", f"<s{msgs[-1]['s']}>"]
        prefix = fit(prefix, self.model.cfg.max_length - MAX_TOKENS)
        item = {"year": year, "channel": CHANNEL, "context": msgs, "speaker": BOT_SPEAKER, "re": msgs[-1]["s"]}
        seed = random.SystemRandom().randrange(1 << 63) if seed is None else seed
        generator = torch.Generator().manual_seed(seed)
        meow_in = is_meow(msgs[-1]["text"])
        n = MEOW_CANDIDATES if meow_in else CANDIDATES
        echoes = []  # candidates that only copied a chat message ("WHAT?" -> "what????")
        penalize = self.repeat_name_ids(msgs)
        scored = self.scored_candidates(prefix, item, msgs, generator, n, echoes, penalize)
        if not scored:
            scored = self.scored_candidates(prefix, item, msgs, generator, FALLBACK_CANDIDATES, echoes, penalize)
        blocked = {copy_key(m["text"]) for m in msgs}
        if not scored and meow_in:
            variation = self.meow_variation(prefix, msgs[-1]["text"], blocked, generator, penalize)
            if variation:
                scored = [[0.0, variation, tok.decode(split_photo_tokens(tok, variation)).strip()]]
        tries = 0
        while not scored:  # last resort: plain samples at 0.8 / 0.95, still no copies of the chat
            if tries == 5:
                if not echoes:
                    raise ValueError("The model only repeated the chat.")
                # Every candidate was an echo: send the best-scoring one rather than nothing.
                scored = self.score(echoes, item, msgs, prefix)
                break
            tries += 1
            for reply in self.sample(prefix, 1, generator, contrast=0.0, temperature=0.8, top_p=0.95,
                                     penalize=penalize):
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
