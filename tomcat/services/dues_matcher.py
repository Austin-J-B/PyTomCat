from __future__ import annotations
from rapidfuzz import fuzz

def score_name(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b) / 100.0

def score_email(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return 1.0 if a.strip().lower() == b.strip().lower() else 0.0

def score_handle(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return 1.0 if a.lower() == b.lower() else 0.0

def total_score(name_s: float, email_s: float, handle_s: float) -> float:
    return 0.3 * name_s + 0.5 * email_s + 0.2 * handle_s

def pick_best(payment: dict, roster: list[dict]) -> tuple[int | None, float]:
    best_id, best = None, 0.0
    for r in roster:
        s = total_score(
            score_name(payment.get("payer_name"), r.get("name")),
            score_email(payment.get("payer_email"), r.get("email")),
            score_handle(payment.get("payer_handle"), r.get("handle")),
        )
        if s > best:
            best, best_id = s, int(r["discord_id"])
    return best_id, best
