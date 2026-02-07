"""Permission helpers for role-based access checks."""

from __future__ import annotations
from typing import Any


def officer_role_ids(settings: Any) -> list[int]:
    """Return configured officer role IDs (list env + legacy single env)."""
    out: list[int] = []
    seen: set[int] = set()

    try:
        raw_ids = getattr(settings, "officer_role_ids", []) or []
    except Exception:
        raw_ids = []

    for raw in raw_ids:
        try:
            rid = int(raw or 0)
        except Exception:
            rid = 0
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)

    try:
        single = int(getattr(settings, "officer_role_id", 0) or 0)
    except Exception:
        single = 0
    if single and single not in seen:
        out.append(single)
    return out


def primary_officer_role_id(settings: Any) -> int:
    """Return the first configured officer role ID, or 0 if unset."""
    ids = officer_role_ids(settings)
    return int(ids[0]) if ids else 0


def is_officer(member: Any, settings: Any) -> bool:
    """Return True if the member has any configured officer role."""
    try:
        officer_ids = set(officer_role_ids(settings))
        if not officer_ids:
            return False
        roles = getattr(member, "roles", []) or []
        for role in roles:
            try:
                rid = int(getattr(role, "id", 0) or 0)
            except Exception:
                rid = 0
            if rid in officer_ids:
                return True
    except Exception:
        return False
    return False
