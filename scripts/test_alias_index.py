"""Alias matching regression: the cached index must agree with a plain scan.

tomcat/aliases.py resolves cat and station names through a precomputed
_AliasIndex instead of re-normalizing the whole alias table on every call. The
speedup only matters if the answers are identical, so this test keeps a
deliberately naive reference implementation of the original algorithm and
asserts the two agree across every name, alias, nickname and a pile of noise.

Run: python scripts/test_alias_index.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat import aliases as A
from tomcat.aliases import CAT_NAMES, CAT_NICKNAMES, STOPWORDS
from tomcat.stations import station_alias_table

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected != actual:
        FAILURES.append(f"{label}\n    reference={expected!r}\n    index    ={actual!r}")


# --------------------------------------------------------------------------
# Reference implementation: the pre-index algorithm, transcribed verbatim.
# --------------------------------------------------------------------------

def ref_alias_pairs(table: Dict[str, List[str]], isw: bool) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for key, aliases in table.items():
        seen: set[str] = set()
        for alias in list(aliases) + [key]:
            alias_norm = A._norm(alias)
            if not alias_norm or (alias_norm in STOPWORDS and not isw):
                continue
            alias_tokens = [t for t in A._words(alias) if t]
            if alias_tokens and not isw and all(t in STOPWORDS for t in alias_tokens):
                continue
            if alias_norm in seen:
                continue
            seen.add(alias_norm)
            pairs.append((alias_norm, key))
    return pairs


def ref_token_matches(token: str, alias_token: str) -> bool:
    if not token or not alias_token:
        return False
    return token == alias_token or (len(token) >= 4 and alias_token.startswith(token))


def ref_exact_or_prefix(
    table: Dict[str, List[str]], text_norm: str, tokens: Iterable[str], isw: bool
) -> Optional[str]:
    best_key: Optional[str] = None
    best_score: Tuple[int, int] = (-1, -1)
    for key, aliases in table.items():
        for alias in list(aliases) + [key]:
            alias_norm = A._norm(alias)
            if not alias_norm or (alias_norm in STOPWORDS and not isw):
                continue
            alias_tokens = [t for t in A._words(alias) if t]
            if alias_tokens and not isw and all(t in STOPWORDS for t in alias_tokens):
                continue
            if alias_norm == text_norm:
                return key
            if re.search(rf"\b{re.escape(alias_norm)}\b", text_norm):
                score = (len(alias_norm), len(alias_tokens))
                if score > best_score:
                    best_score = score
                    best_key = key
    if best_key:
        return best_key

    key_tokens: Dict[str, List[str]] = {}
    for key, aliases in table.items():
        toks: List[str] = []
        for alias in list(aliases) + [key]:
            for tok in A._words(alias):
                if tok and not (tok in STOPWORDS and not isw):
                    toks.append(tok)
        key_tokens[key] = list(dict.fromkeys(toks))

    hits: Dict[str, int] = {}
    for tok in tokens:
        if len(tok) < 3 or (tok in STOPWORDS and not isw):
            continue
        matched = [k for k, toks in key_tokens.items() if any(ref_token_matches(tok, t) for t in toks)]
        if len(matched) == 1:
            hits[matched[0]] = hits.get(matched[0], 0) + 1
    return next(iter(hits)) if len(hits) == 1 else None


def ref_table(want: str) -> Dict[str, List[str]]:
    return A._merged_cat_aliases() if want == "cat" else A._merged_station_aliases()


def ref_resolve(text: str, want: str, isw: bool) -> Optional[str]:
    text_norm = A._normalize(text)
    if text_norm in STOPWORDS:
        return None
    raw = A._words(text_norm)
    tokens = raw if isw else [t for t in raw if t not in STOPWORDS]
    table = ref_table(want)
    key = ref_exact_or_prefix(table, text_norm, tokens, isw)
    if key:
        return A._display_for(key)
    pairs = ref_alias_pairs(table, isw)
    for cand in [text_norm] + tokens:
        if not cand or (not isw and (cand in STOPWORDS or len(cand) < 4)):
            continue
        match = A.best_match(cand, pairs, threshold=82)
        if match:
            return A._display_for(match[0])
    if want == "cat":
        return A._fallback_lookup_cat(text_norm, tokens) or None
    return None


def ref_resolve_stations(text: str, isw: bool) -> List[str]:
    text_norm = A._norm(text)
    raw = A._words(text)
    tokens = raw if isw else [t for t in raw if t not in STOPWORDS]
    table = ref_table("station")
    found: List[str] = []
    found_keys: set[str] = set()
    padded = f" {text_norm} "
    for key, aliases in table.items():
        for alias in list(aliases) + [key]:
            alias_norm = A._norm(alias)
            if not alias_norm or (alias_norm in STOPWORDS and not isw):
                continue
            alias_tokens = [t for t in A._words(alias) if t]
            if alias_tokens and not isw and all(t in STOPWORDS for t in alias_tokens):
                continue
            if f" {alias_norm} " in padded:
                if key not in found_keys:
                    found_keys.add(key)
                    found.append(A._display_for(key))
                break
    for tok in tokens:
        if tok in STOPWORDS:
            continue
        disp = ref_resolve(tok, "station", isw)
        if disp and disp not in found:
            found.append(disp)
    if not found:
        pairs = ref_alias_pairs(table, isw)
        for cand in tokens:
            if cand in STOPWORDS or (len(cand) < 4 and not isw):
                continue
            match = A.best_match(cand, pairs, threshold=82)
            if match:
                disp = A._display_for(match[0])
                if disp not in found:
                    found.append(disp)
    return found


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------

def build_corpus() -> List[str]:
    texts: List[str] = []
    for name in CAT_NAMES:
        texts += [
            name, name.lower(), name.upper(), name.replace(" ", ""), name.replace("-", " "),
            name + "s", name[:-1] if len(name) > 4 else name,
            f"tomcat show me {name}", f"who is {name}", f"is {name} ok?",
        ]
    for nicks in CAT_NICKNAMES.values():
        for nick in nicks:
            texts += [nick, nick.lower(), f"tomcat show me {nick}"]
    for key, aliases in station_alias_table().items():
        for alias in [key] + list(aliases):
            texts += [alias, alias.title(), f"fed {alias} today", f"{alias} needs food",
                      f"i did {alias} and lot 50"]
    texts += [
        "", " ", "hello", "hello everyone how is it going", "the a an and", "hall", "station",
        "lot", "i fed maverick and central library", "tomcat who is ford f-150",
        "fed west hall today", "did the greens and hop", "mary kay & zen plus west",
        "microwave and snickers both fed", "west", "east", "north campus and erb",
        "bookstore church", "lot50 l50", "show me a picture of miccrowave", "wheres twix",
        "eraser?", "pencil 2 vs pencil", "12345", "!!!", "cat", "cats", "feed", "feeding",
        "who", "what", "when", "tomcat identify this", "tomcat crop", "sub for me",
        "schedule", "profile", "oreo 2", "oreo", "shitbag cuntface", "airbus a320 neo",
        "mr stinky", "notacat", "anyone around to cover my round tonight",
    ]
    return list(dict.fromkeys(texts))


def main() -> int:
    #Resolve dynamic aliases once, up front: the background refresh would
    #otherwise swap the table mid-run and make reference and index disagree
    #for reasons that have nothing to do with the index.
    A._do_dyn_alias_refresh()

    corpus = build_corpus()
    for text in corpus:
        for want in ("cat", "station"):
            for isw in (False, True):
                check(
                    f"resolve_station_or_cat({text!r}, want={want!r}, stopwords={isw})",
                    ref_resolve(text, want, isw),
                    A.resolve_station_or_cat(text, want=want, include_stopword_aliases=isw),
                )
        for isw in (False, True):
            check(
                f"resolve_stations({text!r}, stopwords={isw})",
                ref_resolve_stations(text, isw),
                A.resolve_stations(text, include_stopword_aliases=isw),
            )

    #alias_vocab is cached; it must still reflect the live tables.
    vocab = A.alias_vocab()
    cat_keys = set(A._CAT_ALIASES) | set(A._DYN_CAT_ALIASES)
    station_keys = set(station_alias_table())
    check("alias_vocab cats", sorted({A._display_for(k) for k in cat_keys}), vocab["cats"])
    check("alias_vocab stations", sorted({A._display_for(k) for k in station_keys}), vocab["stations"])
    check("alias_vocab all",
          sorted({A._display_for(k) for k in (cat_keys | station_keys)}), vocab["all"])
    assert A.alias_vocab() is vocab, "alias_vocab should return its cached dict"

    #A table change must invalidate the cached index, not serve a stale one.
    saved = (dict(A._DYN_CAT_ALIASES), dict(A._DYN_DISPLAY))
    before = A.resolve_station_or_cat("quantumcat", want="cat")
    A._swap_dyn_aliases({**saved[0], "quantumcat": ["quantumcat"]},
                        {**saved[1], "quantumcat": "QuantumCat"})
    check("index invalidates on alias swap", "QuantumCat",
          A.resolve_station_or_cat("quantumcat", want="cat"))
    check("vocab invalidates on alias swap", True, "QuantumCat" in A.alias_vocab()["cats"])
    A._swap_dyn_aliases(*saved)
    check("resolution restored after swap back", before,
          A.resolve_station_or_cat("quantumcat", want="cat"))

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} mismatch(es) over {len(corpus)} texts")
        for line in FAILURES[:20]:
            print(" -", line)
        return 1
    print(f"OK: alias index matches the reference scan across {len(corpus)} texts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
