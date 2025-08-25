# tomcat/aliases.py
from __future__ import annotations
import re
from typing import Dict, List, Optional, Iterable, Tuple

# One canonical place for both cat and station aliases.
# Fill these with your real data pulled from v5.6 config.js.
# All keys must be lowercase; values are canonical display strings.

_CAT_ALIASES = {
    "microwave": ["microwave", "mike", "mikey"],
    "twix": ["twix", "twixie", "snickers", "snickerdoodle"],   # example mapping
    # ...
}

_STATION_ALIASES = {
    "microwave": ["microwave", "mike", "mikey"],
    "business": ["business", "biz"],
    "hop": ["hop", "house of pizza", "houseofpizza"],
    "greens": ["greens", "the greens", "green lot", "lot green"],
    # ...
}

# Canonical display names (capitalization as you want to show)
_DISPLAY = {
    "microwave": "Microwave",
    "twix": "Twix",
    "business": "Business",
    "hop": "HOP",
    "greens": "Greens",
    # ...
}

STOPWORDS = {"the", "a", "an", "station", "lot", "hall"}


def alias_vocab() -> Dict[str, List[str]]:
    return {
        "cats": sorted({ _DISPLAY.get(k, k.capitalize()) for k in _CAT_ALIASES.keys() }),
        "stations": sorted({ _DISPLAY.get(k, k.capitalize()) for k in _STATION_ALIASES.keys() }),
        "all": sorted({ _DISPLAY.get(k, k.capitalize()) for k in set(list(_CAT_ALIASES.keys()) + list(_STATION_ALIASES.keys())) }),
    }

_WS = re.compile(r"\s+")
def norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower().strip())

def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower().strip())

def _words(s: str) -> List[str]:
    return [w for w in re.split(r"[^a-z0-9]+", _norm(s)) if w]

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def resolve_station_or_cat(text: str, want: str) -> Optional[str]:
    text = _normalize(text)
    if want == "cat":
        for key, vals in _CAT_ALIASES.items():
            for v in vals:
                if re.search(rf"\b{re.escape(v)}\b", text):
                    return _DISPLAY.get(key, key.capitalize())
    else:
        for key, vals in _STATION_ALIASES.items():
            for v in vals:
                if re.search(rf"\b{re.escape(v)}\b", text):
                    return _DISPLAY.get(key, key.capitalize())
    return None
