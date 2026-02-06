"""Permission helpers for role-based access checks."""

from __future__ import annotations
from typing import Any


def is_officer(member: Any, settings: Any) -> bool:
    """Return True if the member has the officer role."""
    try:
        officer_role_id = int(getattr(settings, "officer_role_id", 0) or 0)
        if not officer_role_id:
            return False
        roles = getattr(member, "roles", []) or []
        for role in roles:
            try:
                rid = int(getattr(role, "id", 0) or 0)
            except Exception:
                rid = 0
            if rid == officer_role_id:
                return True
    except Exception:
        return False
    return False
