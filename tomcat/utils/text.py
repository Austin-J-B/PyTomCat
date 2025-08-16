from __future__ import annotations

def norm_alnum_lower(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())