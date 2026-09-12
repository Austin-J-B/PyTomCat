"""Thin wrapper around RapidFuzz/difflib for consistent fuzzy matching."""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

try:
    from rapidfuzz import fuzz as _rf_fuzz  #type: ignore
    from rapidfuzz import process as _rf_process  #type: ignore
    from rapidfuzz.distance import Levenshtein as _rf_levenshtein  #type: ignore
except Exception:  #pragma: no cover - rapidfuzz optional
    _rf_fuzz = None
    _rf_process = None
    _rf_levenshtein = None

import difflib


def _ratio(a_norm: str, b_norm: str) -> int:
    """Score two already-normalized (stripped, lowercased) strings."""
    if not a_norm or not b_norm:
        return 0
    if _rf_fuzz is not None:
        try:
            return int(_rf_fuzz.token_set_ratio(a_norm, b_norm))
        except Exception:
            pass
    return int(round(100 * difflib.SequenceMatcher(None, a_norm, b_norm).ratio()))


def fuzzy_ratio(a: str, b: str) -> int:
    """Return 0-100 similarity score between two strings."""
    return _ratio((a or "").strip().lower(), (b or "").strip().lower())


class FuzzyIndex:
    """Alias/key columns split once, for repeated queries against one table.

    best_match() re-splits its candidate pairs on every call. Callers that query
    the same table many times per message (alias resolution walks every word of
    the text) should build this once and reuse it.
    """

    __slots__ = ("aliases", "keys")

    def __init__(self, candidates: Iterable[Tuple[str, str]]):
        self.aliases: List[str] = []
        self.keys: List[str] = []
        for alias, key in candidates:
            alias_norm = (alias or "").strip().lower()
            if not alias_norm:
                continue
            self.aliases.append(alias_norm)
            self.keys.append(key)

    def best(self, query: str, *, threshold: int = 80) -> Optional[Tuple[str, int]]:
        """Return (canonical_key, score) for the best match at or above threshold.

        Ties go to the earliest candidate and scores are floored to ints, so the
        result matches a plain Python scan; RapidFuzz only moves the scoring loop
        into C.
        """
        q = (query or "").strip().lower()
        if not q or not self.aliases:
            return None

        if _rf_process is not None:
            try:
                #extract() keeps only scores at or above the cutoff. Re-apply the
                #int floor, then take the earliest candidate holding the top score.
                hits = _rf_process.extract(
                    q, self.aliases, scorer=_rf_fuzz.token_set_ratio,
                    limit=None, score_cutoff=float(threshold),
                )
                if not hits:
                    return None
                top = max(int(score) for _a, score, _i in hits)
                idx = min(i for _a, score, i in hits if int(score) == top)
                return self.keys[idx], top
            except Exception:
                pass

        best_key: Optional[str] = None
        best_score = threshold
        for alias_norm, key in zip(self.aliases, self.keys):
            score = _ratio(q, alias_norm)
            if score > best_score or (score == best_score and best_key is None):
                best_key = key
                best_score = score
        return None if best_key is None else (best_key, best_score)


def best_match(
    query: str,
    candidates: Sequence[Tuple[str, str]],
    *,
    threshold: int = 80,
) -> Optional[Tuple[str, int]]:
    """One-shot best fuzzy match over (alias, canonical) pairs."""
    return FuzzyIndex(candidates).best(query, threshold=threshold)


def any_match(query: str, targets: Iterable[str], *, threshold: int = 80) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return False
    return any(_ratio(q, (t or "").strip().lower()) >= threshold for t in targets)


def levenshtein_distance(a: str, b: str) -> int:
    """Compute Levenshtein distance for short tokens."""
    a_norm = (a or "").strip().lower()
    b_norm = (b or "").strip().lower()
    if a_norm == b_norm:
        return 0
    if not a_norm:
        return len(b_norm)
    if not b_norm:
        return len(a_norm)
    if _rf_levenshtein is not None:
        try:
            return int(_rf_levenshtein.distance(a_norm, b_norm))
        except Exception:
            pass
    #difflib has no edit distance; fall back to the classic two-row DP.
    prev = list(range(len(b_norm) + 1))
    for i, ca in enumerate(a_norm, start=1):
        cur = [i]
        for j, cb in enumerate(b_norm, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
