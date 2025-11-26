"""Thin wrapper around RapidFuzz/difflib for consistent fuzzy matching."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

try:
    from rapidfuzz import fuzz as _rf_fuzz  #type: ignore
except Exception:  #pragma: no cover - rapidfuzz optional
    _rf_fuzz = None

import difflib


def fuzzy_ratio(a: str, b: str) -> int:
    """Return 0-100 similarity score between two strings."""
    a_norm = (a or "").strip().lower()
    b_norm = (b or "").strip().lower()
    if not a_norm or not b_norm:
        return 0
    if _rf_fuzz is not None:
        try:
            return int(_rf_fuzz.token_set_ratio(a_norm, b_norm))
        except Exception:
            pass
    return int(round(100 * difflib.SequenceMatcher(None, a_norm, b_norm).ratio()))


def best_match(
    query: str,
    candidates: Sequence[Tuple[str, str]],
    *,
    threshold: int = 80,
) -> Optional[Tuple[str, int]]:
    """Return (canonical_key, score) of best fuzzy match over (alias, canonical) pairs."""
    q = (query or "").strip()
    if not q:
        return None
    best_key: Optional[str] = None
    best_score = threshold
    q_lower = q.lower()
    for alias, key in candidates:
        alias_norm = (alias or "").strip()
        if not alias_norm:
            continue
        score = fuzzy_ratio(q_lower, alias_norm)
        if score > best_score or (score == best_score and best_key is None):
            best_key = key
            best_score = score
    if best_key is None:
        return None
    return best_key, best_score


def any_match(query: str, targets: Iterable[str], *, threshold: int = 80) -> bool:
    for target in targets:
        if fuzzy_ratio(query, target) >= threshold:
            return True
    return False


def levenshtein_distance(a: str, b: str) -> int:
    """Compute simple Levenshtein distance for short tokens."""
    a_norm = (a or "").strip().lower()
    b_norm = (b or "").strip().lower()
    if a_norm == b_norm:
        return 0
    if not a_norm:
        return len(b_norm)
    if not b_norm:
        return len(a_norm)
    rows = len(a_norm) + 1
    cols = len(b_norm) + 1
    dp = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dp[i][0] = i
    for j in range(cols):
        dp[0][j] = j
    for i in range(1, rows):
        ca = a_norm[i - 1]
        for j in range(1, cols):
            cb = b_norm[j - 1]
            cost = 0 if ca == cb else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,      #deletion
                dp[i][j - 1] + 1,      #insertion
                dp[i - 1][j - 1] + cost,  #substitution
            )
    return dp[-1][-1]
