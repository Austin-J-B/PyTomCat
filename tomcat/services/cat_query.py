"""Deterministic CatDatabase query engine used by local LLM fallback routes."""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..aliases import resolve_station_or_cat, resolve_stations
from . import local_photos

_CSV_PATHS = [
    Path("cache/catabase/Catabase - CatDatabase.csv"),
    Path("Catabase - CatDatabase.csv"),
]
_PROFILE_CACHE_PATHS = [Path("cache/catabase/profiles.json")]
_ACTIVE_LOOKBACK_DAYS = 90

_BROWN_POS = ("brown", "tan", "buff", "gold", "cinnamon", "chocolate")
_ORANGE_POS = ("orange", "ginger", "rust", "amber", "peach", "creamsicle")
_GRAY_POS = ("gray", "grey", "silver", "smoke", "charcoal", "ash")
_WHITE_POS = ("white", "snow", "cream", "ivory")
_EYE_COLOR_POS = ("orange", "amber", "yellow", "green", "blue", "gold", "golden", "hazel")
_ALLOWED_RECENT_SCOPES = {"active", "inactive", "all"}
_PHOTO_COUNTS_CACHE: Optional[Dict[str, int]] = None
_PHOTO_COUNTS_CACHE_SIG: Tuple[Tuple[str, float, int], ...] = ()
_CAT_QUERY_TOPIC_RE = re.compile(
    r"\b(cat|cats|catabase|cat\s*database|tnr(?:'d|d)?|tabby|tabbies|brown|gray|grey|orange|ginger|black|white|tuxedo|neuter(?:ed)?|spay(?:ed)?|fixed|location|lot\s*\d+|recently\s+seen|active|inactive|born|birthday|birth\s+year|photos?|pics?|pictures?|live|lives)\b"
)
_CAT_QUERY_INTENT_RE = re.compile(r"\b(which|what|who|how\s+many|list|count|have|has|need(?:s)?)\b|\?")
_CAT_QUERY_CONSTRAINT_RE = re.compile(
    r"\b(with|where|at|in|between|exactly|more\s+than|less\s+than|over|under|at\s+least|at\s+most)\b"
)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _display_name(full: str) -> str:
    txt = _clean_text(full)
    m = re.match(r"^\s*\d+\.\s*(.+)$", txt)
    return (m.group(1) if m else txt).strip()


def _norm_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean_text(value).lower())


def _find_idx(header: List[str], *candidates: str, default: int = -1) -> int:
    idx = {_norm_header(h): i for i, h in enumerate(header)}
    for key in candidates:
        found = idx.get(_norm_header(key))
        if found is not None:
            return int(found)
    return default


def _split_locations(raw: str) -> List[str]:
    if not raw:
        return []
    text = raw.replace("&", ",").replace("/", ",").replace(";", ",")
    text = re.sub(r"\band\b", ",", text, flags=re.I)
    out = []
    for part in text.split(","):
        p = _clean_text(part).strip(" .!?")
        if p:
            out.append(p)
    return out


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    txt = _clean_text(value).lower()
    if not txt:
        return None
    if txt in {"true", "yes", "y", "1"}:
        return True
    if txt in {"false", "no", "n", "0"}:
        return False
    return None


def _coerce_int(
    value: Any,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            parsed = int(value)
        else:
            txt = _clean_text(value)
            if not txt:
                return None
            m = re.search(r"-?\d[\d,]*", txt)
            if not m:
                return None
            parsed = int(m.group(0).replace(",", ""))
    except Exception:
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _is_recently_seen(value: Any) -> Optional[bool]:
    txt = _clean_text(value).lower()
    if not txt:
        return None
    if txt in {"true", "yes", "y", "1", "recent", "recently seen", "active"}:
        return True
    if txt in {"false", "no", "n", "0", "inactive", "not recently seen"}:
        return False
    return _coerce_bool(txt)


def _canonical_recent_scope(value: Any) -> Optional[str]:
    txt = _clean_text(value).lower()
    if not txt:
        return None
    if txt in _ALLOWED_RECENT_SCOPES:
        return txt
    if txt in {"recent", "recently_seen", "recently-seen", "active_only", "active-only"}:
        return "active"
    if txt in {"not_recent", "not-recent", "not_recently_seen", "inactive_only", "inactive-only"}:
        return "inactive"
    if txt in {"any", "everything", "total", "all_cats", "all-cats"}:
        return "all"
    return None


def _row_matches_recent_scope(row_recent: Optional[bool], scope: Optional[str]) -> bool:
    if scope in {None, "all"}:
        return True
    if row_recent is None:
        # Degrade gracefully when the recency column is missing/invalid.
        return True
    if scope == "active":
        return row_recent is True
    if scope == "inactive":
        return row_recent is False
    return True


def _is_tnrd(value: str) -> bool:
    txt = _clean_text(value).lower()
    return txt in {"yes", "y", "true", "1", "tnrd", "tnr'd"}


def _is_tnrd_unknown(value: Any) -> bool:
    txt = _clean_text(value).lower()
    if not txt:
        return False
    return bool(re.search(r"we\s*do\s*(?:not|n['’]?t)\s*do\s*that", txt))


def _prepare_color_text(text: str) -> str:
    t = _clean_text(text).lower()
    if not t:
        return ""
    eye_group = "|".join(re.escape(c) for c in _EYE_COLOR_POS)
    t = re.sub(rf"\b(?:{eye_group})\s+eyes?\b", " ", t)
    t = re.sub(rf"\beyes?\s*(?:are|is|look|looks|with)?\s*(?:{eye_group})\b", " ", t)
    return _clean_text(t).lower()


def _canonical_color(value: Any) -> Optional[str]:
    txt = _clean_text(value).lower()
    if not txt:
        return None
    if txt in {"black_white", "black and white", "black/white", "black-white", "tuxedo"}:
        return "black_white"
    if txt in {"orange", "ginger", "rust"}:
        return "orange"
    if txt in {"tabby", "tabbies"}:
        return "tabby"
    if txt in {"brown", "tan", "buff", "gold"}:
        return "brown"
    if txt in {"gray", "grey", "silver", "smoke"}:
        return "gray"
    if txt in {"white", "snow", "cream", "ivory"}:
        return "white"
    if re.search(r"\bblack\s*(?:and|&|/)\s*white\b|\btuxedo\b", txt):
        return "black_white"
    if any(token in txt for token in _ORANGE_POS):
        return "orange"
    if any(token in txt for token in _GRAY_POS):
        return "gray"
    if "tabby" in txt or "tabbies" in txt:
        return "tabby"
    if any(token in txt for token in _BROWN_POS):
        return "brown"
    if any(token in txt for token in _WHITE_POS):
        return "white"
    return None


def _matches_brown(text: str) -> bool:
    t = _prepare_color_text(text)
    if not t:
        return False
    if "gray tabby" in t or "grey tabby" in t:
        return False
    if any(token in t for token in _ORANGE_POS):
        return False
    if any(token in t for token in _BROWN_POS):
        return True
    if "tabby" in t:
        if any(token in t for token in _GRAY_POS):
            return False
        if any(token in t for token in _ORANGE_POS):
            return False
        return True
    return False


def _matches_gray(text: str) -> bool:
    t = _prepare_color_text(text)
    if not t:
        return False
    if any(token in t for token in _GRAY_POS):
        return True
    if "tabby" in t and ("gray" in t or "grey" in t):
        return True
    return False


def _matches_orange(text: str) -> bool:
    t = _prepare_color_text(text)
    if not t:
        return False
    strong = (
        r"\b(?:orange|ginger|rusty?)\s+(?:tabby|tuxedo|tortoiseshell|calico|cat|kitty|kitten|guy|baby|coat|fur|longhair|shorthair)\b",
        r"\b(?:tabby|tuxedo|tortoiseshell|calico)\s+(?:orange|ginger)\b",
        r"\b(?:orange|ginger)\s+and\s+(?:black|white|gray|grey|brown)\b",
        r"\b(?:gray|grey|black|white|brown)\s*/\s*(?:orange|ginger)\b",
        r"\b(?:orange|ginger)\s*/\s*(?:gray|grey|black|white|brown)\b",
        r"\bsolid\s+(?:orange|ginger)\b",
        r"^\s*(?:orange|ginger)\b",
    )
    if any(re.search(pat, t) for pat in strong):
        return True
    if "calico" in t and any(token in t for token in ("orange", "ginger")):
        return True
    if any(token in t for token in _ORANGE_POS):
        if re.search(r"\bhints?\s+of\s+orange\b", t):
            return False
        if re.search(r"\baround\s+(?:his|her|their|its)\s+(?:muzzle|face)\b", t):
            return False
        if re.search(r"\blooks?\s+[^.]{0,30}\borange\b", t):
            return False
        return True
    return False


def _matches_black_white(text: str) -> bool:
    t = _prepare_color_text(text)
    if not t:
        return False
    if "tuxedo" in t:
        return True
    if re.search(r"\bblack\s*(?:and|&|/)\s*white\b", t):
        return True
    return ("black" in t and "white" in t)


def _matches_tabby(text: str) -> bool:
    t = _prepare_color_text(text)
    if not t:
        return False
    return ("tabby" in t or "tabbies" in t)


def _matches_white(text: str) -> bool:
    t = _prepare_color_text(text)
    if not t:
        return False
    # "black and white" / tuxedo cats are not "white cats"
    if re.search(r"\bblack\s*(?:and|&|/)\s*white\b|\btuxedo\b", t):
        return False
    if any(token in t for token in _WHITE_POS):
        return True
    return False


def _parse_date(value: Any) -> Optional[date]:
    txt = _clean_text(value)
    if not txt:
        return None
    t = txt.lower()
    if t in {"#n/a", "n/a", "na", "unknown"}:
        return None
    t = re.sub(r"^[^0-9]+", "", t).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except Exception:
            continue
    return None


def _birth_year(value: Any) -> Optional[int]:
    parsed_date = _parse_date(value)
    if parsed_date is not None:
        return parsed_date.year
    direct = _coerce_int(value, minimum=1900, maximum=2100)
    if direct is not None:
        return direct
    txt = _clean_text(value)
    if not txt:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", txt)
    if not m:
        return None
    return _coerce_int(m.group(1), minimum=1900, maximum=2100)


def _photo_count(value: Any) -> Optional[int]:
    txt = _clean_text(value)
    if not txt:
        return None
    return _coerce_int(txt.replace(",", ""), minimum=0)


def _is_recent_by_last_seen(last_seen_date: Any) -> Optional[bool]:
    seen_date = _parse_date(last_seen_date)
    if seen_date is None:
        return None
    cutoff = date.today() - timedelta(days=_ACTIVE_LOOKBACK_DAYS)
    return seen_date >= cutoff


def _header_quality_score(header: List[str]) -> int:
    score = 0
    if _find_idx(header, "location") >= 0:
        score += 1
    if _find_idx(header, "tnrd", "tnr'd?", "tnrd?") >= 0:
        score += 1
    if _find_idx(header, "physical description", "physical") >= 0:
        score += 1
    if _find_idx(header, "recently seen?", "recently seen", "recently_seen") >= 0:
        score += 1
    if _find_idx(header, "last seen date", "last seen", "last_seen_date") >= 0:
        score += 1
    if _find_idx(header, "birthday estimate", "birthday", "birth year") >= 0:
        score += 1
    if _find_idx(header, "number of pics", "number of photos", "photo count") >= 0:
        score += 1
    return score


def _load_rows_from_profile_cache(path: Path) -> Tuple[List[str], List[List[str]]]:
    if not path.exists():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        return [], []

    header = ["Full Name", "Location", "Physical Description", "TNR'd?", "Last seen date", "Birthday estimate", "Number of pics"]
    rows: List[List[str]] = []
    photo_counts = _load_photo_counts()
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        full_name = _clean_text(profile.get("actual_name"))
        if not full_name:
            continue
        count = photo_counts.get(_norm_name(_display_name(full_name)))
        rows.append(
            [
                full_name,
                _clean_text(profile.get("location")),
                _clean_text(profile.get("physical_description")),
                _clean_text(profile.get("tnrd")),
                _clean_text(profile.get("last_seen_date")),
                _clean_text(profile.get("birthday_estimate")),
                str(count) if count is not None else "",
            ]
        )
    return header, rows


def _load_rows() -> Tuple[List[str], List[List[str]]]:
    candidates: List[Tuple[int, int, List[str], List[List[str]]]] = []

    for path in _CSV_PATHS:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
            if rows and rows[0]:
                header, data_rows = rows[0], rows[1:]
                candidates.append((_header_quality_score(header), len(data_rows), header, data_rows))
        except Exception:
            continue

    for path in _PROFILE_CACHE_PATHS:
        header, rows = _load_rows_from_profile_cache(path)
        if header and rows:
            candidates.append((_header_quality_score(header), len(rows), header, rows))

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, _, best_header, best_rows = candidates[0]
        return best_header, best_rows

    return [], []


def _photo_source_signature(paths: List[Path]) -> Tuple[Tuple[str, float, int], ...]:
    sig: List[Tuple[str, float, int]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            stat = path.stat()
            sig.append((str(path.resolve()), float(stat.st_mtime), int(stat.st_size)))
        except Exception:
            continue
    return tuple(sig)


def _split_photo_name_cells(raw: str) -> List[str]:
    txt = _clean_text(raw)
    if not txt:
        return []
    # Local metadata uses "|" for multi-cat tags and sometimes comma-delimited ID labels.
    parts = [p.strip() for p in txt.split("|") if _clean_text(p)]
    if len(parts) == 1 and "," in txt and re.search(r"\d+\.\s*", txt):
        parts = [p.strip() for p in txt.split(",") if _clean_text(p)]
    return parts


def _load_photo_counts() -> Dict[str, int]:
    global _PHOTO_COUNTS_CACHE, _PHOTO_COUNTS_CACHE_SIG
    sig = _photo_source_signature([local_photos.metadata_csv_path()])
    if _PHOTO_COUNTS_CACHE is not None and sig == _PHOTO_COUNTS_CACHE_SIG:
        return dict(_PHOTO_COUNTS_CACHE)

    counts: Dict[str, int] = {}
    try:
        for row in local_photos.read_metadata_rows():
            if not isinstance(row, dict):
                continue
            raw_cell = row.get("Box Cat IDs") or ""
            for raw_name in _split_photo_name_cells(raw_cell):
                display = _display_name(raw_name)
                key = _norm_name(display)
                if key:
                    counts[key] = counts.get(key, 0) + 1
    except Exception:
        pass

    _PHOTO_COUNTS_CACHE = dict(counts)
    _PHOTO_COUNTS_CACHE_SIG = sig
    return counts


def _lookup_photo_count(*names: Any) -> Optional[int]:
    """Resolve a numeric photo count from local metadata using any known cat label."""
    counts = _load_photo_counts()
    for name in names:
        key = _norm_name(_display_name(name))
        if not key:
            continue
        value = counts.get(key)
        if value is not None:
            return value
    return None


def _canonical_station(raw_location: Optional[str]) -> Optional[str]:
    txt = _clean_text(raw_location)
    if not txt:
        return None
    resolved = resolve_station_or_cat(txt, want="station", include_stopword_aliases=True)
    return resolved or txt


def _row_matches_station(row_location: str, station: str) -> bool:
    if not station:
        return True
    row_loc = _clean_text(row_location)
    if not row_loc:
        return False
    target = _clean_text(station).lower()
    for part in _split_locations(row_loc):
        resolved = resolve_station_or_cat(part, want="station", include_stopword_aliases=True)
        if resolved and _clean_text(resolved).lower() == target:
            return True
        if _clean_text(part).lower() == target:
            return True
    return target in row_loc.lower()


def _filter_phrase(
    location: Optional[str],
    tnrd: Optional[bool],
    color_family: Optional[str],
    birth_year: Optional[int],
    photo_count_min: Optional[int],
    photo_count_max: Optional[int],
) -> str:
    sections: List[str] = []
    if location:
        sections.append(f"at {location}")
    attrs: List[str] = []
    if tnrd is True:
        attrs.append("TNR'd")
    elif tnrd is False:
        attrs.append("not TNR'd")
    if color_family:
        attrs.append("black and white" if color_family == "black_white" else color_family)
    if attrs:
        sections.append("that are " + " and ".join(attrs))
    if birth_year is not None:
        sections.append(f"born in {birth_year}")
    if photo_count_min is not None or photo_count_max is not None:
        if photo_count_min is not None and photo_count_max is not None:
            if photo_count_min == photo_count_max:
                sections.append(f"with exactly {photo_count_min} photos")
            else:
                sections.append(f"with between {photo_count_min} and {photo_count_max} photos")
        elif photo_count_min is not None:
            sections.append(f"with at least {photo_count_min} photos")
        elif photo_count_max is not None:
            sections.append(f"with at most {photo_count_max} photos")
    return " ".join(sections).strip()


def _scope_prefix(scope: Optional[str], *, explicit: bool) -> str:
    if not explicit:
        return ""
    if scope == "active":
        return "active "
    if scope == "inactive":
        return "inactive "
    return ""


_PLAN_OP_ALIASES = {
    "=": "eq",
    "==": "eq",
    "eq": "eq",
    "equals": "eq",
    "!=": "neq",
    "<>": "neq",
    "neq": "neq",
    "not_eq": "neq",
    ">": "gt",
    "gt": "gt",
    "greater_than": "gt",
    ">=": "gte",
    "gte": "gte",
    "ge": "gte",
    "at_least": "gte",
    "<": "lt",
    "lt": "lt",
    "less_than": "lt",
    "<=": "lte",
    "lte": "lte",
    "le": "lte",
    "at_most": "lte",
    "contains": "contains",
    "not_contains": "not_contains",
    "starts_with": "starts_with",
    "ends_with": "ends_with",
    "month_eq": "month_eq",
    "year_eq": "year_eq",
    "between": "between",
    "is_true": "is_true",
    "is_false": "is_false",
    "is_empty": "is_empty",
    "is_not_empty": "is_not_empty",
}

_MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_MONTH_NAME_BY_INT = {
    1: "january",
    2: "february",
    3: "march",
    4: "april",
    5: "may",
    6: "june",
    7: "july",
    8: "august",
    9: "september",
    10: "october",
    11: "november",
    12: "december",
}


def _normalize_plan_op(op: Any) -> Optional[str]:
    txt = _clean_text(op).lower().replace(" ", "_")
    if not txt:
        return None
    return _PLAN_OP_ALIASES.get(txt)


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        txt = _clean_text(value).replace(",", "")
        if not txt:
            return None
        return float(txt)
    except Exception:
        return None


def _month_int(value: Any) -> Optional[int]:
    n = _coerce_int(value, minimum=1, maximum=12)
    if n is not None:
        return n
    txt = _clean_text(value).lower()
    if not txt:
        return None
    return _MONTH_MAP.get(txt)


def _resolve_column_index(header: List[str], hint: Any, default: int = 0) -> int:
    if not header:
        return default
    name = _clean_text(hint)
    if not name:
        return default
    hint_norm = _norm_header(name)
    if hint_norm in {"name", "fullname", "cat", "catname"}:
        return default
    norm_to_idx = {_norm_header(col): idx for idx, col in enumerate(header)}
    exact = norm_to_idx.get(hint_norm)
    if exact is not None:
        return int(exact)
    # "contains" fallback over normalized names.
    for norm_col, idx in norm_to_idx.items():
        if hint_norm and hint_norm in norm_col:
            return int(idx)
    for norm_col, idx in norm_to_idx.items():
        if norm_col and norm_col in hint_norm:
            return int(idx)
    return default


def _friendly_column_name(value: Any) -> str:
    txt = _clean_text(value)
    if not txt:
        return "value"
    txt = re.sub(r"\?+$", "", txt).strip()
    return txt.lower() or "value"


def _is_photo_count_column(value: Any) -> bool:
    norm = _norm_header(_clean_text(value))
    if not norm:
        return False
    return norm in {"numberofpics", "numberofphotos", "photocount", "piccount", "imagecount"}


def _display_filter_value(value: Any) -> str:
    txt = _clean_text(value)
    if txt:
        return txt
    if value is None:
        return "null"
    return str(value)


def _display_month(value: Any) -> str:
    month_num = _month_int(value)
    if month_num is None:
        return _display_filter_value(value).lower()
    return _MONTH_NAME_BY_INT.get(month_num, str(month_num))


def _describe_plan_filter(column: str, op: str, value: Any, value2: Any) -> str:
    col = _friendly_column_name(column)
    v1 = _display_filter_value(value)
    v2 = _display_filter_value(value2)
    if _is_photo_count_column(column):
        if op == "eq":
            return f"have exactly {v1} photos"
        if op == "neq":
            return f"do not have exactly {v1} photos"
        if op == "gt":
            return f"have more than {v1} photos"
        if op == "gte":
            return f"have at least {v1} photos"
        if op == "lt":
            return f"have fewer than {v1} photos"
        if op == "lte":
            return f"have at most {v1} photos"
        if op == "between":
            return f"have between {v1} and {v2} photos"

    if op == "eq":
        return f"{col} is {v1}"
    if op == "neq":
        return f"{col} is not {v1}"
    if op == "gt":
        return f"{col} is greater than {v1}"
    if op == "gte":
        return f"{col} is at least {v1}"
    if op == "lt":
        return f"{col} is less than {v1}"
    if op == "lte":
        return f"{col} is at most {v1}"
    if op == "contains":
        return f"{col} contains {v1}"
    if op == "not_contains":
        return f"{col} does not contain {v1}"
    if op == "starts_with":
        return f"{col} starts with {v1}"
    if op == "ends_with":
        return f"{col} ends with {v1}"
    if op == "month_eq":
        return f"{col} is in {_display_month(value)}"
    if op == "year_eq":
        return f"{col} is in {v1}"
    if op == "is_true":
        return f"{col} is true"
    if op == "is_false":
        return f"{col} is false"
    if op == "is_empty":
        return f"{col} is empty"
    if op == "is_not_empty":
        return f"{col} is not empty"
    if op == "between":
        return f"{col} is between {v1} and {v2}"
    return f"{col} matches {v1}"


def _describe_plan_filters(plan_filters: List[Tuple[int, str, str, Any, Any]], logical: str) -> str:
    if not plan_filters:
        return ""
    connector = " and " if logical == "and" else " or "
    parts = [_describe_plan_filter(col, op, value, value2) for _idx, col, op, value, value2 in plan_filters]
    parts = [_clean_text(p) for p in parts if _clean_text(p)]
    return connector.join(parts)


def _qualify_conditions(conditions: str) -> str:
    cond = _clean_text(conditions)
    if not cond:
        return ""
    if re.match(r"^(have|are|were|do|need|needs|contain|contains|match|matches)\b", cond):
        return f"that {cond}"
    return f"whose {cond}"


def _photo_count_extreme_from_text(text: str) -> Optional[str]:
    txt = _clean_text(text).lower()
    if not txt:
        return None
    max_patterns = (
        r"\b(?:most|highest|max(?:imum)?)\s+(?:number\s+of\s+)?(?:photos?|pics?|pictures?)\b",
        r"\b(?:photos?|pics?|pictures?)\s+(?:count\s+)?(?:is|are)?\s*(?:highest|max(?:imum)?)\b",
    )
    min_patterns = (
        r"\b(?:fewest|least|lowest|min(?:imum)?)\s+(?:number\s+of\s+)?(?:photos?|pics?|pictures?)\b",
        r"\b(?:photos?|pics?|pictures?)\s+(?:count\s+)?(?:is|are)?\s*(?:lowest|min(?:imum)?)\b",
    )
    if any(re.search(p, txt) for p in max_patterns):
        return "max"
    if any(re.search(p, txt) for p in min_patterns):
        return "min"
    return None


def _photo_count_lookup_query_from_text(text: str) -> Optional[Dict[str, Any]]:
    txt = _clean_text(text)
    if not txt:
        return None
    subject_patterns = (
        r"\bhow\s+many\s+(?:photos?|pics?|pictures?)\s+does\s+(.+?)\s+have\b",
        r"\bhow\s+many\s+(?:photos?|pics?|pictures?)\s+of\s+(.+?)\s+(?:are\s+there|exist)\b",
        r"\bhow\s+many\s+(?:photos?|pics?|pictures?)\s+(?:are\s+there\s+)?of\s+(.+?)\b",
    )
    raw_subject = ""
    for pat in subject_patterns:
        m = re.search(pat, txt, flags=re.I)
        if not m:
            continue
        raw_subject = _clean_text(m.group(1)).strip(" ?!.,;:")
        if raw_subject:
            break
    if not raw_subject:
        return None
    resolved_subject = resolve_station_or_cat(
        raw_subject,
        want="cat",
        include_stopword_aliases=True,
    ) or raw_subject
    return {
        "op": "list_names_by_filters",
        "result": "list",
        "logical": "and",
        "select_column": "Number of pics",
        "limit": 1,
        "response_mode": "cat_photo_count",
        "subject_hint": resolved_subject,
        "filters": [
            {"column": "Full Name", "op": "contains", "value": resolved_subject},
        ],
    }


def _compare_eq(cell: Any, value: Any) -> bool:
    left_num = _to_float(cell)
    right_num = _to_float(value)
    if left_num is not None and right_num is not None:
        return left_num == right_num
    left_date = _parse_date(cell)
    right_date = _parse_date(value)
    if left_date is not None and right_date is not None:
        return left_date == right_date
    left_bool = _coerce_bool(cell)
    right_bool = _coerce_bool(value)
    if left_bool is not None and right_bool is not None:
        return left_bool is right_bool
    return _clean_text(cell).lower() == _clean_text(value).lower()


def _evaluate_plan_filter(cell: Any, op: str, value: Any, value2: Any = None) -> bool:
    text = _clean_text(cell)
    low = text.lower()
    normalized_op = _normalize_plan_op(op)
    if not normalized_op:
        return False
    if normalized_op == "is_empty":
        return text == ""
    if normalized_op == "is_not_empty":
        return text != ""
    if normalized_op == "is_true":
        return _coerce_bool(cell) is True
    if normalized_op == "is_false":
        return _coerce_bool(cell) is False
    if normalized_op == "contains":
        return _clean_text(value).lower() in low
    if normalized_op == "not_contains":
        return _clean_text(value).lower() not in low
    if normalized_op == "starts_with":
        return low.startswith(_clean_text(value).lower())
    if normalized_op == "ends_with":
        return low.endswith(_clean_text(value).lower())
    if normalized_op == "month_eq":
        d = _parse_date(cell)
        m = _month_int(value)
        return d is not None and m is not None and d.month == m
    if normalized_op == "year_eq":
        d = _parse_date(cell)
        y = _coerce_int(value, minimum=1900, maximum=2100)
        return d is not None and y is not None and d.year == y
    if normalized_op in {"eq", "neq"}:
        is_eq = _compare_eq(cell, value)
        return is_eq if normalized_op == "eq" else (not is_eq)

    left_num = _to_float(cell)
    right_num = _to_float(value)
    if normalized_op in {"gt", "gte", "lt", "lte"} and left_num is not None and right_num is not None:
        if normalized_op == "gt":
            return left_num > right_num
        if normalized_op == "gte":
            return left_num >= right_num
        if normalized_op == "lt":
            return left_num < right_num
        return left_num <= right_num

    left_date = _parse_date(cell)
    right_date = _parse_date(value)
    if normalized_op in {"gt", "gte", "lt", "lte"} and left_date is not None and right_date is not None:
        if normalized_op == "gt":
            return left_date > right_date
        if normalized_op == "gte":
            return left_date >= right_date
        if normalized_op == "lt":
            return left_date < right_date
        return left_date <= right_date

    if normalized_op == "between":
        left_n = _to_float(cell)
        low_n = _to_float(value)
        high_n = _to_float(value2)
        if left_n is not None and low_n is not None and high_n is not None:
            lo, hi = sorted((low_n, high_n))
            return lo <= left_n <= hi
        left_d = _parse_date(cell)
        low_d = _parse_date(value)
        high_d = _parse_date(value2)
        if left_d is not None and low_d is not None and high_d is not None:
            lo, hi = sorted((low_d, high_d))
            return lo <= left_d <= hi
        return False

    return False


def _run_generic_plan_query(query: Dict[str, Any], header: List[str], rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    filters_raw = (query or {}).get("filters")
    if not isinstance(filters_raw, list) or not filters_raw:
        return None
    full_idx = 0
    logical = _clean_text((query or {}).get("logical")).lower()
    if logical not in {"and", "or"}:
        logical = "and"
    plan_filters: List[Tuple[int, str, str, Any, Any]] = []
    for item in filters_raw:
        if not isinstance(item, dict):
            continue
        op = _normalize_plan_op(item.get("op"))
        col_hint = item.get("column")
        if not op or not _clean_text(col_hint):
            continue
        idx = _resolve_column_index(header, col_hint, default=full_idx)
        col_name = (
            header[idx] if (0 <= idx < len(header) and _clean_text(header[idx])) else _clean_text(col_hint)
        )
        plan_filters.append((idx, col_name, op, item.get("value"), item.get("value2")))
    if not plan_filters:
        return None

    matched_rows: List[List[str]] = []
    for row in rows:
        if not row:
            continue
        checks: List[bool] = []
        for idx, _col_name, op, value, value2 in plan_filters:
            cell = row[idx] if 0 <= idx < len(row) else ""
            checks.append(_evaluate_plan_filter(cell, op, value, value2))
        if (logical == "and" and all(checks)) or (logical == "or" and any(checks)):
            matched_rows.append(row)

    result_mode = _clean_text((query or {}).get("result")).lower()
    if result_mode not in {"list", "count"}:
        op_hint = _clean_text((query or {}).get("op")).lower()
        result_mode = "count" if op_hint in {"count_all_cats", "count_by_filters"} else "list"

    count = len(matched_rows)
    conditions = _describe_plan_filters(plan_filters, logical)
    qualified_conditions = _qualify_conditions(conditions)
    select_idx = _resolve_column_index(header, (query or {}).get("select_column"), default=full_idx)
    limit = _coerce_int((query or {}).get("limit"), minimum=1, maximum=200) or 120
    response_mode = _clean_text((query or {}).get("response_mode")).lower()
    if response_mode == "cat_photo_count":
        subject_hint = _clean_text((query or {}).get("subject_hint")) or "that cat"
        if not matched_rows:
            message = f"I couldn't find a cat named {subject_hint} in the catabase."
            return {
                "ok": True,
                "op": "generic_plan",
                "count": 0,
                "names": [],
                "filters": {"logical": logical, "filters": filters_raw},
                "message": message,
            }
        row0 = matched_rows[0]
        full_name = row0[full_idx] if 0 <= full_idx < len(row0) else subject_hint
        cat_name = _display_name(full_name) or subject_hint
        raw_value = row0[select_idx] if 0 <= select_idx < len(row0) else ""
        value_num = _photo_count(raw_value)
        if value_num is None:
            value_num = _lookup_photo_count(full_name, cat_name, subject_hint)
        if value_num is not None:
            message = f"{cat_name} has {value_num} photos in the catabase."
            out_values = [str(value_num)]
        else:
            message = f"I couldn't find a photo count for {cat_name}."
            out_values = []
        return {
            "ok": True,
            "op": "generic_plan",
            "count": count,
            "names": out_values,
            "filters": {"logical": logical, "filters": filters_raw},
            "message": message,
        }
    values: List[str] = []
    if result_mode == "list":
        seen: set[str] = set()
        for row in matched_rows:
            raw = row[select_idx] if 0 <= select_idx < len(row) else ""
            if select_idx == full_idx:
                raw = _display_name(raw)
            val = _clean_text(raw)
            if not val:
                continue
            key = val.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(val)
            if len(values) >= limit:
                break

    if result_mode == "count":
        if qualified_conditions:
            message = f"There are {count} cats {qualified_conditions} in the catabase."
        else:
            message = f"There are {count} cats in the catabase."
    elif not values:
        if qualified_conditions:
            message = f"I couldn't find any cats {qualified_conditions} in the catabase."
        else:
            message = "I couldn't find any cats in the catabase."
    else:
        joined = ", ".join(values)
        if qualified_conditions:
            message = f"The cats {qualified_conditions} are: {joined}."
        else:
            message = f"The cats are: {joined}."
        if len(matched_rows) > len(values):
            message += f" (+{len(matched_rows) - len(values)} more)"

    return {
        "ok": True,
        "op": "generic_plan",
        "count": count,
        "names": values if result_mode == "list" else [],
        "filters": {"logical": logical, "filters": filters_raw},
        "message": message,
    }


def _infer_generic_plan_from_text(text: str, header: List[str]) -> Optional[Dict[str, Any]]:
    txt = _clean_text(text).lower()
    if not txt:
        return None

    filters: List[Dict[str, Any]] = []
    missing_column: Optional[str] = None

    photos_idx = _resolve_column_index(header, "Number of pics", default=-1)
    if photos_idx >= 0:
        photos_col = header[photos_idx] if photos_idx < len(header) else "Number of pics"
        m_exact = re.search(r"\bexactly\s+(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b", txt)
        if m_exact:
            n = _coerce_int(m_exact.group(1), minimum=0)
            if n is not None:
                op = "eq"
                tail = txt[m_exact.end() : m_exact.end() + 18]
                if re.search(r"\b(?:and|or)\s+more\b", tail):
                    op = "gte"
                filters.append({"column": photos_col, "op": op, "value": n})
        else:
            m_gt = re.search(r"\b(?:more\s+than|over|greater\s+than|above)\s+(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b", txt)
            if m_gt:
                n = _coerce_int(m_gt.group(1), minimum=0)
                if n is not None:
                    filters.append({"column": photos_col, "op": "gt", "value": n})
            m_gte = re.search(r"\b(?:at\s+least|no\s+less\s+than|minimum(?:\s+of)?)\s+(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b", txt)
            if m_gte and not any(f["column"] == photos_col for f in filters):
                n = _coerce_int(m_gte.group(1), minimum=0)
                if n is not None:
                    filters.append({"column": photos_col, "op": "gte", "value": n})
            m_lt = re.search(r"\b(?:less\s+than|under|fewer\s+than|below)\s+(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b", txt)
            if m_lt:
                n = _coerce_int(m_lt.group(1), minimum=0)
                if n is not None:
                    filters.append({"column": photos_col, "op": "lt", "value": n})
            m_lte = re.search(r"\b(?:at\s+most|no\s+more\s+than|maximum(?:\s+of)?)\s+(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b", txt)
            if m_lte and not any(f["column"] == photos_col and f["op"] in {"lt", "lte"} for f in filters):
                n = _coerce_int(m_lte.group(1), minimum=0)
                if n is not None:
                    filters.append({"column": photos_col, "op": "lte", "value": n})

    month_match = re.search(
        r"\b(?:in|during|for)\s+(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sept|sep|october|oct|november|nov|december|dec)\b",
        txt,
    )
    if month_match:
        date_hint = "Last seen date"
        if "born" in txt or "birthday" in txt or "birth year" in txt:
            date_hint = "Birthday estimate"
        date_idx = _resolve_column_index(header, date_hint, default=-1)
        if date_idx < 0:
            #Fallback to first date-looking column we know.
            for hint in ("Last seen date", "Birthday estimate"):
                idx = _resolve_column_index(header, hint, default=-1)
                if idx >= 0:
                    date_idx = idx
                    break
        if date_idx >= 0:
            date_col = header[date_idx] if date_idx < len(header) else date_hint
            filters.append({"column": date_col, "op": "month_eq", "value": month_match.group(1)})

    # Generic "which cats have a <column> of <value>" inference using actual CSV headers.
    # Example: "which cats have a favorite number of 5"
    generic_eq = re.search(
        r"\b(?:which|what|who)\s+cats?\s+have\s+(?:a|an|the)?\s*([a-z][a-z0-9' \-]{1,60})\s+(?:of|is|=|equal(?:s)?\s+to)\s+([a-z0-9][a-z0-9' .,\-/]{0,60})\b",
        txt,
    )
    if generic_eq:
        raw_col = _clean_text(generic_eq.group(1)).strip(" ,.-")
        raw_val = _clean_text(generic_eq.group(2)).strip(" ,.-")
        if raw_col and raw_val:
            col_idx = _resolve_column_index(header, raw_col, default=-1)
            if col_idx >= 0:
                col_name = header[col_idx] if col_idx < len(header) else raw_col
                if not any(_norm_header(f.get("column")) == _norm_header(col_name) for f in filters if isinstance(f, dict)):
                    filters.append({"column": col_name, "op": "eq", "value": raw_val})
            else:
                missing_column = raw_col

    # Fallback: "which cats have/are <trait>" → search Physical Description.
    # Catches queries like "which cats have spots", "which cats are fluffy", etc.
    if not filters and not missing_column:
        phys_idx = _resolve_column_index(header, "Physical Description", default=-1)
        if phys_idx >= 0:
            phys_col = header[phys_idx] if phys_idx < len(header) else "Physical Description"
            trait_m = re.search(
                r"\b(?:which|what|who)\s+cats?\s+(?:have|are|look|with)\s+(.{2,40}?)(?:\?|$)",
                txt,
            )
            if trait_m:
                trait = trait_m.group(1).strip(" ,.-?")
                if trait:
                    filters.append({"column": phys_col, "op": "contains", "value": trait})

    if not filters:
        if missing_column:
            return {
                "op": "list_names_by_filters",
                "result": "list",
                "logical": "and",
                "select_column": "Full Name",
                "filters": [],
                "missing_column": missing_column,
            }
        return None
    return {
        "op": "list_names_by_filters",
        "result": "list",
        "logical": "and",
        "select_column": "Full Name",
        "filters": filters,
    }


def _query_has_non_photo_structured_constraints(query: Dict[str, Any]) -> bool:
    q = query or {}
    if _clean_text(q.get("location")):
        return True
    if _coerce_bool(q.get("tnrd")) is not None:
        return True
    if _canonical_color(q.get("color_family")) is not None:
        return True
    if _coerce_int(q.get("birth_year"), minimum=1900, maximum=2100) is not None:
        return True
    if _canonical_recent_scope(q.get("recent_scope")) is not None:
        return True
    return False


def run_cat_query(query: Dict[str, Any]) -> Dict[str, Any]:
    """Execute deterministic query against the local CatDatabase CSV."""
    op = _clean_text((query or {}).get("op")).lower() or "list_names_by_filters"
    if op not in {"count_all_cats", "count_by_filters", "list_names_by_filters"}:
        op = "list_names_by_filters"

    header, rows = _load_rows()
    generic_result = _run_generic_plan_query(query or {}, header, rows)
    if generic_result is not None:
        return generic_result
    source_text = _clean_text((query or {}).get("source_text"))
    if source_text and not _query_has_non_photo_structured_constraints(query or {}):
        inferred_plan = _infer_generic_plan_from_text(source_text, header)
        if inferred_plan is not None:
            missing_column = _clean_text(inferred_plan.get("missing_column"))
            if missing_column:
                return {
                    "ok": True,
                    "op": "generic_plan",
                    "count": 0,
                    "names": [],
                    "filters": {"logical": "and", "filters": []},
                    "message": f"I couldn't find a column named {missing_column} in the catabase.",
                }
            op_hint = _clean_text((query or {}).get("op")).lower()
            if op_hint in {"count_all_cats", "count_by_filters"}:
                inferred_plan["op"] = "count_by_filters"
                inferred_plan["result"] = "count"
            plan_result = _run_generic_plan_query(inferred_plan, header, rows)
            if plan_result is not None:
                return plan_result

    full_idx = 0
    location_idx = _find_idx(header, "location")
    tnrd_idx = _find_idx(header, "tnrd", "tnr'd?", "tnrd?")
    physical_idx = _find_idx(header, "physical description", "physical")
    birthday_idx = _find_idx(header, "birthday estimate", "birthday", "birth year")
    photo_count_idx = _find_idx(header, "number of pics", "number of photos", "photo count")
    recent_idx = _find_idx(header, "recently seen?", "recently seen", "recently_seen")
    last_seen_idx = _find_idx(header, "last seen date", "last seen", "last_seen_date")

    requested_location = _canonical_station((query or {}).get("location"))
    requested_tnrd = _coerce_bool((query or {}).get("tnrd"))
    requested_color = _canonical_color((query or {}).get("color_family"))
    requested_birth_year = _coerce_int((query or {}).get("birth_year"), minimum=1900, maximum=2100)
    requested_photo_min = _coerce_int((query or {}).get("photo_count_min"))
    requested_photo_max = _coerce_int((query or {}).get("photo_count_max"))
    if requested_photo_min is not None and requested_photo_min < 0:
        requested_photo_min = 0
    requested_photo_extreme = _clean_text((query or {}).get("photo_count_extreme")).lower() or None
    if requested_photo_extreme not in {"max", "min"}:
        requested_photo_extreme = None
    if (
        requested_photo_min is not None
        and requested_photo_max is not None
        and requested_photo_min > requested_photo_max
        and requested_photo_max >= 0
    ):
        requested_photo_min, requested_photo_max = requested_photo_max, requested_photo_min
    recent_scope_raw = (query or {}).get("recent_scope")
    requested_recent_scope = _canonical_recent_scope(recent_scope_raw)
    has_explicit_recent_scope = recent_scope_raw is not None and requested_recent_scope is not None

    # Avoid dumping every cat name when a filter parse misses all slots.
    _all_filters_empty = (
        not requested_location
        and requested_tnrd is None
        and not requested_color
        and requested_birth_year is None
        and requested_photo_min is None
        and requested_photo_max is None
        and requested_photo_extreme is None
        and not has_explicit_recent_scope
    )
    if op == "list_names_by_filters" and _all_filters_empty:
        # If source_text is present, the user asked something specific that we
        # couldn't parse — return an honest failure instead of a misleading count.
        if source_text:
            return {
                "ok": True,
                "op": "list_names_by_filters",
                "count": 0,
                "names": [],
                "filters": {},
                "message": "I wasn't able to understand that query.",
            }
        op = "count_all_cats"

    effective_recent_scope = requested_recent_scope
    if op in {"count_by_filters", "list_names_by_filters"} and effective_recent_scope is None:
        # "most/least photos" queries should consider all cats by default.
        if requested_photo_extreme in {"max", "min"}:
            effective_recent_scope = "all"
        else:
            effective_recent_scope = "active"

    # Global recency queries should answer with a count rather than dumping names.
    if (
        op == "list_names_by_filters"
        and effective_recent_scope in {"active", "inactive"}
        and not requested_location
        and requested_tnrd is None
        and not requested_color
        and requested_birth_year is None
        and requested_photo_min is None
        and requested_photo_max is None
        and requested_photo_extreme is None
    ):
        op = "count_by_filters"

    total_count = 0
    recent_count = 0
    filtered_names: List[str] = []
    extreme_candidates: List[Tuple[str, int]] = []

    recency_column_present = recent_idx >= 0
    last_seen_column_present = last_seen_idx >= 0
    photo_counts_by_name: Optional[Dict[str, int]] = None
    if (
        (
            requested_photo_min is not None
            or requested_photo_max is not None
            or requested_photo_extreme is not None
        )
        and photo_count_idx < 0
    ):
        photo_counts_by_name = _load_photo_counts()

    for row in rows:
        if not row or full_idx >= len(row):
            continue
        full_name = _clean_text(row[full_idx])
        if not full_name:
            continue
        display = _display_name(full_name)
        if not display:
            continue
        total_count += 1
        if recency_column_present:
            row_recent = _is_recently_seen(row[recent_idx] if 0 <= recent_idx < len(row) else "")
            if row_recent is None:
                # Blank/unknown recency cells are treated as inactive.
                row_recent = False
        elif last_seen_column_present:
            row_recent = _is_recent_by_last_seen(row[last_seen_idx] if 0 <= last_seen_idx < len(row) else "")
            if row_recent is None:
                row_recent = False
        else:
            row_recent = None
        if row_recent is True:
            recent_count += 1

        if op == "count_all_cats":
            continue

        if not _row_matches_recent_scope(row_recent, effective_recent_scope):
            continue

        if requested_location is not None:
            row_loc = row[location_idx] if 0 <= location_idx < len(row) else ""
            if not _row_matches_station(row_loc, requested_location):
                continue

        if requested_tnrd is not None:
            row_tnrd = row[tnrd_idx] if 0 <= tnrd_idx < len(row) else ""
            if _is_tnrd_unknown(row_tnrd):
                continue
            if _is_tnrd(row_tnrd) != requested_tnrd:
                continue

        if requested_color is not None:
            color_text = row[physical_idx] if 0 <= physical_idx < len(row) else ""
            if requested_color == "brown":
                if not _matches_brown(color_text):
                    continue
            elif requested_color == "gray":
                if not _matches_gray(color_text):
                    continue
            elif requested_color == "orange":
                if not _matches_orange(color_text):
                    continue
            elif requested_color == "black_white":
                if not _matches_black_white(color_text):
                    continue
            elif requested_color == "tabby":
                if not _matches_tabby(color_text):
                    continue
            elif requested_color == "white":
                if not _matches_white(color_text):
                    continue

        if requested_birth_year is not None:
            row_birth_year = _birth_year(row[birthday_idx] if 0 <= birthday_idx < len(row) else "")
            if row_birth_year != requested_birth_year:
                continue

        row_photos: Optional[int] = None
        if (
            requested_photo_min is not None
            or requested_photo_max is not None
            or requested_photo_extreme is not None
        ):
            if 0 <= photo_count_idx < len(row):
                row_photos = _photo_count(row[photo_count_idx])
            else:
                row_photos = (photo_counts_by_name or {}).get(_norm_name(display))
            if row_photos is None:
                continue
            if requested_photo_min is not None and row_photos < requested_photo_min:
                continue
            if requested_photo_max is not None and row_photos > requested_photo_max:
                continue

        if requested_photo_extreme in {"max", "min"}:
            if row_photos is None:
                continue
            extreme_candidates.append((display, row_photos))
            continue

        filtered_names.append(display)

    if op == "count_all_cats":
        count = total_count
        message = (
            f"There are {count} total cats in the catabase, and {recent_count} of them "
            "have been recently seen."
        )
        return {
            "ok": True,
            "op": op,
            "count": count,
            "recent_count": recent_count,
            "names": [],
            "filters": {},
            "message": message,
        }

    if requested_photo_extreme in {"max", "min"}:
        scope_prefix = _scope_prefix(effective_recent_scope, explicit=has_explicit_recent_scope)
        phrase = _filter_phrase(
            requested_location,
            requested_tnrd,
            requested_color,
            requested_birth_year,
            requested_photo_min,
            requested_photo_max,
        )
        if not extreme_candidates:
            if phrase:
                message = f"I couldn't find any {scope_prefix}cats {phrase} with a photo count."
            else:
                message = f"I couldn't find any {scope_prefix}cats with a photo count."
            return {
                "ok": True,
                "op": "photo_count_extreme",
                "count": 0,
                "names": [],
                "filters": {
                    "location": requested_location,
                    "tnrd": requested_tnrd,
                    "color_family": requested_color,
                    "birth_year": requested_birth_year,
                    "photo_count_min": requested_photo_min,
                    "photo_count_max": requested_photo_max,
                    "photo_count_extreme": requested_photo_extreme,
                    "recent_scope": effective_recent_scope,
                },
                "message": message,
            }

        extreme_value = (
            max(v for _, v in extreme_candidates)
            if requested_photo_extreme == "max"
            else min(v for _, v in extreme_candidates)
        )
        names: List[str] = []
        seen: set[str] = set()
        for nm, val in extreme_candidates:
            if val != extreme_value:
                continue
            key = _norm_name(nm)
            if key in seen:
                continue
            seen.add(key)
            names.append(nm)
        rank_label = "highest" if requested_photo_extreme == "max" else "lowest"
        if op == "count_by_filters":
            if phrase:
                message = (
                    f"There are {len(names)} {scope_prefix}cats with the {rank_label} photo count "
                    f"({extreme_value}) {phrase} in the catabase."
                )
            else:
                message = (
                    f"There are {len(names)} {scope_prefix}cats with the {rank_label} photo count "
                    f"({extreme_value}) in the catabase."
                )
            out_names: List[str] = []
        elif len(names) == 1:
            if phrase:
                message = (
                    f"{names[0]} has the {rank_label} photo count ({extreme_value}) "
                    f"{phrase} in the catabase."
                )
            else:
                message = f"{names[0]} has the {rank_label} photo count ({extreme_value}) in the catabase."
            out_names = names
        else:
            joined = ", ".join(names)
            if phrase:
                message = (
                    f"The cats tied for the {rank_label} photo count ({extreme_value}) "
                    f"{phrase} are: {joined}."
                )
            else:
                message = (
                    f"The cats tied for the {rank_label} photo count ({extreme_value}) are: {joined}."
                )
            out_names = names
        return {
            "ok": True,
            "op": "photo_count_extreme",
            "count": len(names),
            "names": out_names,
            "filters": {
                "location": requested_location,
                "tnrd": requested_tnrd,
                "color_family": requested_color,
                "birth_year": requested_birth_year,
                "photo_count_min": requested_photo_min,
                "photo_count_max": requested_photo_max,
                "photo_count_extreme": requested_photo_extreme,
                "recent_scope": effective_recent_scope,
            },
            "message": message,
        }

    count = len(filtered_names)
    scope_prefix = _scope_prefix(effective_recent_scope, explicit=has_explicit_recent_scope)
    phrase = _filter_phrase(
        requested_location,
        requested_tnrd,
        requested_color,
        requested_birth_year,
        requested_photo_min,
        requested_photo_max,
    )

    if op == "count_by_filters":
        if phrase:
            message = f"There are {count} {scope_prefix}cats {phrase} in the catabase."
        else:
            message = f"There are {count} {scope_prefix}cats in the catabase."
        return {
            "ok": True,
            "op": op,
            "count": count,
            "names": [],
            "filters": {
                "location": requested_location,
                "tnrd": requested_tnrd,
                "color_family": requested_color,
                "birth_year": requested_birth_year,
                "photo_count_min": requested_photo_min,
                "photo_count_max": requested_photo_max,
                "photo_count_extreme": requested_photo_extreme,
                "recent_scope": effective_recent_scope,
            },
            "message": message,
        }

    if not filtered_names:
        if phrase:
            message = f"I couldn't find any {scope_prefix}cats {phrase}."
        else:
            message = f"I couldn't find any {scope_prefix}cats matching that filter."
    else:
        joined = ", ".join(filtered_names)
        if phrase:
            message = f"The {scope_prefix}cats {phrase} are: {joined}."
        else:
            message = f"The {scope_prefix}cats are: {joined}."

    return {
        "ok": True,
        "op": op,
        "count": count,
        "names": filtered_names,
        "filters": {
            "location": requested_location,
            "tnrd": requested_tnrd,
            "color_family": requested_color,
            "birth_year": requested_birth_year,
            "photo_count_min": requested_photo_min,
            "photo_count_max": requested_photo_max,
            "photo_count_extreme": requested_photo_extreme,
            "recent_scope": effective_recent_scope,
        },
        "message": message,
    }


def infer_query_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort deterministic query inference used when local LLM parsing times out."""
    txt = _clean_text(text).lower()
    if not txt:
        return None

    if not looks_like_cat_query_text(txt):
        return None

    photo_lookup_query = _photo_count_lookup_query_from_text(text)
    if photo_lookup_query is not None:
        return photo_lookup_query

    op = "list_names_by_filters"
    if re.search(r"\bhow\s+many\b", txt) or re.search(r"\bcount\b", txt):
        op = "count_by_filters"

    recent_scope: Optional[str] = None
    if re.search(r"\b(?:all\s+cats|including\s+inactive|include\s+inactive|regardless\s+of\s+(?:recently\s+seen|active\s+status)|include\s+all)\b", txt):
        recent_scope = "all"
    elif re.search(r"\b(?:inactive|not\s+recently\s+seen|not\s+active|haven['’]?t\s+been\s+seen|have\s+not\s+been\s+seen)\b", txt):
        recent_scope = "inactive"
    elif re.search(r"\b(?:recently\s+seen|active)\b", txt):
        recent_scope = "active"

    birth_year: Optional[int] = None
    birth_match = re.search(
        r"\b(?:born|birthday(?:\s+estimate)?|birth\s+year)\s*(?:in|during|from|is|was)?\s*(19\d{2}|20\d{2})\b",
        txt,
    )
    if not birth_match:
        birth_match = re.search(
            r"\b(19\d{2}|20\d{2})\s+(?:born|birth(?:day|\s+year)?)\b",
            txt,
        )
    if birth_match:
        birth_year = _coerce_int(birth_match.group(1), minimum=1900, maximum=2100)

    location: Optional[str] = None
    color_or_trait_phrase_re = re.compile(
        r"\b(?:black|white|orange|ginger|gray|grey|silver|smoke|brown|tan|buff|gold|tabby|tabbies|tuxedo|inactive|active|recent(?:ly)?|tnr(?:'d|d)?|neuter(?:ed)?|spay(?:ed)?|fixed)\b"
    )
    generic_cat_phrase_tokens = {
        "which",
        "what",
        "who",
        "how",
        "many",
        "list",
        "show",
        "tell",
        "me",
        "all",
        "the",
        "those",
        "these",
        "any",
        "have",
        "has",
        "with",
        "without",
        "still",
        "need",
        "needs",
        "to",
        "be",
        "that",
        "and",
        "or",
        "more",
        "less",
        "than",
        "at",
        "least",
        "most",
        "photos",
        "pics",
        "pictures",
        "born",
        "in",
        "during",
    }
    lot_match = re.search(r"\blot\s*-?\s*(\d{1,3})\b", txt)
    if lot_match:
        location = resolve_station_or_cat(
            f"lot {lot_match.group(1)}",
            want="station",
            include_stopword_aliases=True,
        ) or f"Lot {lot_match.group(1)}"
    else:
        loc_match = re.search(
            r"\b(?:at|in|on|by|near|around)\s+([a-z0-9' -]{2,40})\b",
            txt,
        )
        if loc_match:
            snippet = _clean_text(loc_match.group(1))
            if snippet:
                snippet = re.split(
                    r"\b(?:that|who|which|have|has|with|born|photos?|pics?|pictures?)\b",
                    snippet,
                    maxsplit=1,
                )[0].strip(" ,.-")
                if (
                    not re.search(r"\b(cat|cats|catabase|database|system)\b", snippet)
                    and not re.fullmatch(r"(19\d{2}|20\d{2})", snippet)
                    and (birth_year is None or snippet != str(birth_year))
                ):
                    location = resolve_station_or_cat(
                        snippet,
                        want="station",
                        include_stopword_aliases=True,
                    ) or None
        location_hint = bool(re.search(r"\b(?:lot\s*-?\s*\d{1,3}|location|located)\b", txt))
        if not location and location_hint:
            stations = resolve_stations(txt, include_stopword_aliases=True) or []
            if stations:
                location = stations[0]
        if not location:
            # Support adjective-style station phrases like "bookstore cats".
            for m in re.finditer(r"\b([a-z0-9' -]{2,40})\s+cats?\b", txt):
                cand = _clean_text(m.group(1)).strip(" ,.-")
                if not cand:
                    continue
                cand = re.sub(
                    r"^(?:which|what|who|how\s+many|list|show|tell\s+me|all|the|those|these|any)\b\s*",
                    "",
                    cand,
                    flags=re.I,
                ).strip(" ,.-")
                if not cand:
                    continue
                cand_tokens = [tok for tok in re.findall(r"[a-z0-9']+", cand.lower()) if tok]
                meaningful = [
                    tok
                    for tok in cand_tokens
                    if len(tok) >= 3 and tok not in generic_cat_phrase_tokens
                ]
                if not meaningful:
                    continue
                if color_or_trait_phrase_re.search(cand):
                    continue
                if re.fullmatch(r"(19\d{2}|20\d{2})", cand):
                    continue
                if birth_year is not None and cand == str(birth_year):
                    continue
                resolved = resolve_station_or_cat(
                    cand,
                    want="station",
                    include_stopword_aliases=True,
                )
                if resolved:
                    location = resolved
                    break

    tnrd: Optional[bool] = None
    tnr_terms = r"(?:tnr(?:'d|d)?|neuter(?:ed)?|spay(?:ed)?|fixed|alter(?:ed)?|steriliz(?:ed|e))"
    if re.search(rf"\b{tnr_terms}\b", txt):
        needs_tnr = bool(
            re.search(rf"\b(?:still\s+)?need(?:s|ing)?\s+(?:to\s+be\s+)?{tnr_terms}\b", txt)
            or re.search(rf"\b(?:needs?|requires?)\s+{tnr_terms}\b", txt)
            or re.search(r"\bun(?:neutered|spayed|fixed)\b", txt)
        )
        explicit_not_tnr = bool(re.search(rf"\b(?:not|isn['’]?t|without|non)\s+{tnr_terms}\b", txt))
        tnrd = False if (needs_tnr or explicit_not_tnr) else True

    color_family: Optional[str] = None
    if re.search(r"\bblack\s*(?:and|&|/)\s*white\b|\btuxedo\b", txt):
        color_family = "black_white"
    elif re.search(r"\b(?:tabby|tabbies)\b", txt):
        color_family = "tabby"
    elif re.search(r"\b(gray|grey|silver|smoke)\b", txt):
        color_family = "gray"
    elif re.search(r"\b(orange|ginger|rust)\b", txt):
        color_family = "orange"
    elif re.search(r"\b(brown|tan|buff|gold)\b", txt):
        color_family = "brown"
    elif re.search(r"\b(white|snow|cream|ivory)\b", txt):
        # Only match standalone white, not "black and white"
        if not re.search(r"\bblack\s*(?:and|&|/)\s*white\b", txt):
            color_family = "white"

    photo_count_min: Optional[int] = None
    photo_count_max: Optional[int] = None
    photo_count_extreme = _photo_count_extreme_from_text(txt)
    if re.search(r"\b(?:photos?|pics?|pictures?)\b", txt):
        range_match = re.search(
            r"\bbetween\s+(\d[\d,]*)\s+and\s+(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b",
            txt,
        )
        if range_match:
            low = _coerce_int(range_match.group(1), minimum=0)
            high = _coerce_int(range_match.group(2), minimum=0)
            if low is not None and high is not None:
                photo_count_min = min(low, high)
                photo_count_max = max(low, high)
        else:
            strict_low_match = re.search(
                r"\b(?:more\s+than|over|greater\s+than|above)\s+((?<!\d)-?\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if strict_low_match:
                n = _coerce_int(strict_low_match.group(1))
                if n is not None:
                    photo_count_min = max(0, n + 1)
            low_match = re.search(
                r"\b(?:at\s+least|no\s+less\s+than|minimum(?:\s+of)?)\s+((?<!\d)-?\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if low_match and photo_count_min is None:
                n = _coerce_int(low_match.group(1))
                if n is not None:
                    photo_count_min = max(0, n)
            plus_match = re.search(
                r"\b(\d[\d,]*)\s*\+\s*(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if plus_match and photo_count_min is None:
                photo_count_min = _coerce_int(plus_match.group(1), minimum=0)
            or_more_match = re.search(
                r"\b(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\s+(?:or\s+more|and\s+up)\b",
                txt,
            )
            if or_more_match and photo_count_min is None:
                photo_count_min = _coerce_int(or_more_match.group(1), minimum=0)

            strict_high_match = re.search(
                r"\b(?:less\s+than|under|fewer\s+than|below)\s+((?<!\d)-?\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if strict_high_match:
                n = _coerce_int(strict_high_match.group(1))
                if n is not None:
                    photo_count_max = n - 1
            high_match = re.search(
                r"\b(?:at\s+most|no\s+more\s+than|maximum(?:\s+of)?)\s+((?<!\d)-?\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if high_match and photo_count_max is None:
                n = _coerce_int(high_match.group(1))
                if n is not None:
                    photo_count_max = n
            or_less_match = re.search(
                r"\b(\d[\d,]*)\s+(?:photos?|pics?|pictures?)\s+(?:or\s+less|or\s+fewer)\b",
                txt,
            )
            if or_less_match and photo_count_max is None:
                photo_count_max = _coerce_int(or_less_match.group(1), minimum=0)

            exact_match = re.search(
                r"\b(?:exactly|with)\s+((?<!\d)-?\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if exact_match and photo_count_min is None and photo_count_max is None:
                n = _coerce_int(exact_match.group(1))
                if n is not None:
                    if n < 0:
                        photo_count_min = 0
                        photo_count_max = -1
                    else:
                        photo_count_min = n
                        photo_count_max = n
            generic_exact_match = re.search(
                r"((?<!\d)-?\d[\d,]*)\s+(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if (
                generic_exact_match
                and photo_count_min is None
                and photo_count_max is None
            ):
                n = _coerce_int(generic_exact_match.group(1))
                if n is not None:
                    if n < 0:
                        photo_count_min = 0
                        photo_count_max = -1
                    else:
                        photo_count_min = n
                        photo_count_max = n
            no_photos_match = re.search(
                r"\b(?:no|zero)\s+(?:photos?|pics?|pictures?)\b|\bwithout\s+(?:any\s+)?(?:photos?|pics?|pictures?)\b",
                txt,
            )
            if (
                no_photos_match
                and photo_count_min is None
                and photo_count_max is None
            ):
                photo_count_min = 0
                photo_count_max = 0
                if recent_scope is None:
                    recent_scope = "all"
    if (
        photo_count_min is not None
        and photo_count_max is not None
        and photo_count_min > photo_count_max
        and photo_count_max >= 0
    ):
        photo_count_min, photo_count_max = photo_count_max, photo_count_min

    if (
        op == "count_by_filters"
        and not location
        and tnrd is None
        and color_family is None
        and birth_year is None
        and photo_count_min is None
        and photo_count_max is None
        and photo_count_extreme is None
        and recent_scope in {None, "all"}
    ):
        op = "count_all_cats"

    if (
        op == "list_names_by_filters"
        and recent_scope in {"active", "inactive"}
        and not location
        and tnrd is None
        and color_family is None
        and birth_year is None
        and photo_count_min is None
        and photo_count_max is None
        and photo_count_extreme is None
    ):
        op = "count_by_filters"

    return {
        "op": op,
        "location": location,
        "tnrd": tnrd,
        "color_family": color_family,
        "birth_year": birth_year,
        "photo_count_min": photo_count_min,
        "photo_count_max": photo_count_max,
        "photo_count_extreme": photo_count_extreme,
        "recent_scope": recent_scope,
    }


def looks_like_cat_query_text(text: str) -> bool:
    txt = _clean_text(text).lower()
    if not txt:
        return False
    if not _CAT_QUERY_TOPIC_RE.search(txt):
        return False
    if _CAT_QUERY_INTENT_RE.search(txt):
        return True
    return bool(_CAT_QUERY_CONSTRAINT_RE.search(txt))
