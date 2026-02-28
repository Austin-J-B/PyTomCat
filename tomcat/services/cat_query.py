"""Deterministic CatDatabase query engine used by local LLM fallback routes."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..aliases import resolve_station_or_cat, resolve_stations

_CSV_PATHS = [Path("Catabase - CatDatabase.csv")]

_BROWN_POS = ("brown", "tan", "buff", "gold", "cinnamon", "chocolate")
_ORANGE_POS = ("orange", "ginger", "rust", "amber", "peach", "creamsicle")
_GRAY_POS = ("gray", "grey", "silver", "smoke", "charcoal", "ash")
_EYE_COLOR_POS = ("orange", "amber", "yellow", "green", "blue", "gold", "golden", "hazel")


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _display_name(full: str) -> str:
    txt = _clean_text(full)
    m = re.match(r"^\s*\d+\.\s*(.+)$", txt)
    return (m.group(1) if m else txt).strip()


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


def _is_tnrd(value: str) -> bool:
    txt = _clean_text(value).lower()
    return txt in {"yes", "y", "true", "1", "tnrd", "tnr'd"}


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


def _load_rows() -> Tuple[List[str], List[List[str]]]:
    for path in _CSV_PATHS:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
            if rows:
                return rows[0], rows[1:]
        except Exception:
            continue
    return [], []


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


def _filter_phrase(location: Optional[str], tnrd: Optional[bool], color_family: Optional[str]) -> str:
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
    return " ".join(sections).strip()


def run_cat_query(query: Dict[str, Any]) -> Dict[str, Any]:
    """Execute deterministic query against the local CatDatabase CSV."""
    op = _clean_text((query or {}).get("op")).lower() or "list_names_by_filters"
    if op not in {"count_all_cats", "count_by_filters", "list_names_by_filters"}:
        op = "list_names_by_filters"

    header, rows = _load_rows()
    full_idx = 0
    location_idx = _find_idx(header, "location")
    tnrd_idx = _find_idx(header, "tnrd", "tnr'd?", "tnrd?")
    physical_idx = _find_idx(header, "physical description", "physical")

    requested_location = _canonical_station((query or {}).get("location"))
    requested_tnrd = _coerce_bool((query or {}).get("tnrd"))
    requested_color = _canonical_color((query or {}).get("color_family"))

    # Avoid dumping every cat name when a filter parse misses all slots.
    if (
        op == "list_names_by_filters"
        and not requested_location
        and requested_tnrd is None
        and not requested_color
    ):
        op = "count_all_cats"

    all_names: List[str] = []
    filtered_names: List[str] = []

    for row in rows:
        if not row or full_idx >= len(row):
            continue
        full_name = _clean_text(row[full_idx])
        if not full_name:
            continue
        display = _display_name(full_name)
        if not display:
            continue
        all_names.append(display)

        if op == "count_all_cats":
            continue

        if requested_location is not None:
            row_loc = row[location_idx] if 0 <= location_idx < len(row) else ""
            if not _row_matches_station(row_loc, requested_location):
                continue

        if requested_tnrd is not None:
            row_tnrd = row[tnrd_idx] if 0 <= tnrd_idx < len(row) else ""
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

        filtered_names.append(display)

    if op == "count_all_cats":
        count = len(all_names)
        message = f"There are {count} cats in the catabase."
        return {
            "ok": True,
            "op": op,
            "count": count,
            "names": [],
            "filters": {},
            "message": message,
        }

    count = len(filtered_names)
    phrase = _filter_phrase(requested_location, requested_tnrd, requested_color)

    if op == "count_by_filters":
        if phrase:
            message = f"There are {count} cats {phrase} in the catabase."
        else:
            message = f"There are {count} cats in the catabase."
        return {
            "ok": True,
            "op": op,
            "count": count,
            "names": [],
            "filters": {
                "location": requested_location,
                "tnrd": requested_tnrd,
                "color_family": requested_color,
            },
            "message": message,
        }

    if not filtered_names:
        if phrase:
            message = f"I couldn't find any cats {phrase}."
        else:
            message = "I couldn't find any cats matching that filter."
    else:
        joined = ", ".join(filtered_names)
        if phrase:
            message = f"The cats {phrase} are: {joined}."
        else:
            message = f"The matching cats are: {joined}."

    return {
        "ok": True,
        "op": op,
        "count": count,
        "names": filtered_names,
        "filters": {
            "location": requested_location,
            "tnrd": requested_tnrd,
            "color_family": requested_color,
        },
        "message": message,
    }


def infer_query_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort deterministic query inference used when local LLM parsing times out."""
    txt = _clean_text(text).lower()
    if not txt:
        return None

    # Only trigger on clearly cat/catabase-related informational queries.
    if not re.search(r"\b(cat|cats|catabase|cat\s*database|tnr(?:'d|d)?|tabby|tabbies|brown|gray|grey|orange|ginger|black|white|tuxedo|neuter(?:ed)?|spay(?:ed)?|fixed|location|lot\s*\d+)\b", txt):
        return None
    if not re.search(r"\b(which|what|who|how\s+many|list|count)\b|\?", txt):
        return None

    op = "list_names_by_filters"
    if re.search(r"\bhow\s+many\b", txt) or re.search(r"\bcount\b", txt):
        op = "count_by_filters"

    location: Optional[str] = None
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
                if not re.search(r"\b(cat|cats|catabase|database|system)\b", snippet):
                    location = resolve_station_or_cat(
                        snippet,
                        want="station",
                        include_stopword_aliases=True,
                    ) or None
        if not location:
            stations = resolve_stations(txt, include_stopword_aliases=True) or []
            if stations:
                location = stations[0]

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

    if op == "count_by_filters" and not location and tnrd is None and color_family is None:
        op = "count_all_cats"

    return {
        "op": op,
        "location": location,
        "tnrd": tnrd,
        "color_family": color_family,
    }
