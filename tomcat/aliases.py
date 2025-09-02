# tomcat/aliases.py
from __future__ import annotations
import re
from typing import Dict, List, Optional, Iterable, Tuple

# One canonical place for both cat and station aliases.
# Fill these with your real data pulled from v5.6 config.js.
# All keys must be lowercase; values are canonical display strings.

# Canonical cat names from prior version. We keep aliases minimal for now (self name + normalized variants).
CAT_NAMES: List[str] = [
    "Microwave", "Twix", "Ford F-150", "Eggs", "Eraser", "Snickers", "Hershey", "Pencil", "Melvin", "Alaska",
    "Laufey", "Faye", "Lionel", "Pencil 2", "Snowball", "Marley", "Bobbie", "Porkchop", "Rolo", "Citlali",
    "Paquini", "Glockenspiel", "Tlacuilo", "Garfield", "Aphrodite", "Tang", "Angel", "Friga", "Ginger",
    "Pepper", "Scraggle", "Noir", "Zee", "Oreo 2", "Stove", "Scringle", "Dingus", "Winston", "Radar",
    "Dumpster", "Gregory", "Rubber", "Bruno", "Shitbag Cuntface", "Boots", "Princess", "Nefarious", "Houdini",
    "Freya", "Thor", "Odin", "Voidling", "Piggy", "Tommy", "Callie", "Lard", "Airbus A320 Neo", "Eden",
    "Creamsicle", "Redacted", "Cassie", "Gorygreg", "Mr Stinky", "NotACat", "Ernie", "Tepi", "Toblerone",
    "Waffles", "Unnamed Noir Child", "Kinder", "Enchilada", "Robin", "Mr Sir", "Coronavirus", "Musketeer",
    "Eezard", "Ooni", "Ed Sheeran", "Leaflet", "Atzi", "Ehecatl", "Tlatecuini", "Mixtli", "Maddox",
    "Pallas", "Honda", "Bandit", "Vincente", "Petal", "Chimichanga", "Butter", "Cloudy", "Meatball", "Itztli",
]

# Optional nickname map you can extend over time (display names as keys)
CAT_NICKNAMES: Dict[str, List[str]] = {
    "Microwave": ["Professor Sprinkles", "Buddy", "Apollo", "Mike", "Michael", "Micro"],
    "Eraser": ["Bacon", "Tuxedo"],
    "Paquini": ["Panini"],
    "Glockenspiel": ["Glock"],
    "Garfield": ["Tito FluffyButt", "Tito"],
    "Aphrodite": ["Dittie"],
    "Stove": ["Squonk"],
    "Scringle": ["Blorbo"],
    "Rubber": ["Stupid"],
    "Nefarious": ["Double Cheeseburger"],
    "Piggy": ["Piggy toes"],
    "Eezard": ["Lizard", "Anole"],
    "Cloudy": ["Cirrus"],
    "Meatball": ["Nimbus"],
}

def _alias_variants(name: str) -> List[str]:
    base = name.lower().strip()
    simple = re.sub(r"\s+", " ", base)
    tight = re.sub(r"[^a-z0-9]+", "", base)
    hyphens = base.replace("-", " ")
    variants = {base, simple, hyphens, tight}
    return [v for v in variants if v]

def _build_cat_aliases() -> Dict[str, List[str]]:
    table: Dict[str, List[str]] = {}
    for disp in CAT_NAMES:
        key = disp.lower()
        vals: List[str] = []
        # canonical name variants
        vals.extend(_alias_variants(disp))
        # nicknames and their token variants
        for nick in CAT_NICKNAMES.get(disp, []):
            vals.extend(_alias_variants(nick))
            # also split multi-words to allow partial tokens (e.g., "tito" from "Tito FluffyButt")
            for tok in re.split(r"[^a-z0-9]+", nick.lower()):
                if tok:
                    vals.extend(_alias_variants(tok))
        # unique preserve order
        seen = set(); out: List[str] = []
        for v in vals:
            if v not in seen:
                seen.add(v); out.append(v)
        table[key] = out
    return table

_CAT_ALIASES: Dict[str, List[str]] = _build_cat_aliases()

_STATION_ALIASES = {
    # Prior config stations
    "west hall": ["west hall", "west", "hall"],
    "maintenance": ["maintenance", "maint"],
    "business": ["business", "coba"],
    "the greens": ["the greens", "greens", "green", "grink", "grinks"],
    "hop": ["hop", "pecan", "thwop", "thop", "heights"],  # Heights on Pecan
    "lot 50": ["lot 50", "lot50", "l50", "lot"],
    "mary kay and zen": ["mary kay and zen", "mkz", "zen", "mary kay", "mary", "kay"],
    # Some stations are also cat names in the list; include them if they are real stations too
    "microwave": ["microwave", "mike", "mikey", "miker", "micro", "wave", "old man", "michael", "him", "himb"],
    "snickers": ["snickers", "snicks"],
}

# Canonical display names (capitalization as you want to show)
_DISPLAY = {
    # Cats (subset will be overridden by alias_vocab() aggregation anyway)
    **{name.lower(): name for name in CAT_NAMES},
    # Stations
    "west hall": "West Hall",
    "maintenance": "Maintenance",
    "business": "Business",
    "the greens": "The Greens",
    "hop": "HOP",
    "lot 50": "Lot 50",
    "mary kay and zen": "Mary Kay and Zen",
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
    """Deterministic resolution: whole-word alias first; else unambiguous prefix of an alias token.
    Supports partial nicknames like 'micro' → Microwave, 'tito' → Garfield.
    """
    text_norm = _normalize(text)
    tokens = set(_words(text_norm))

    def _resolve(table: Dict[str, List[str]]) -> Optional[str]:
        # 1) whole-word alias match
        for key, vals in table.items():
            for v in vals:
                if v and re.search(rf"\b{re.escape(v)}\b", text_norm):
                    return _DISPLAY.get(key, key.capitalize())
        # 2) unambiguous prefix of alias tokens (length ≥3)
        hits: Dict[str, int] = {}
        # Precompute token lists per key
        key_tokens: Dict[str, List[str]] = {}
        for key, vals in table.items():
            toks: List[str] = []
            for v in vals:
                toks.extend(_words(v))
            key_tokens[key] = list({t for t in toks if t})

        for tok in tokens:
            if len(tok) < 3:
                continue
            matched_keys = []
            for key, toks in key_tokens.items():
                if any(t.startswith(tok) for t in toks):
                    matched_keys.append(key)
            if len(matched_keys) == 1:
                k = matched_keys[0]
                hits[k] = hits.get(k, 0) + 1
        if len(hits) == 1:
            only = next(iter(hits.keys()))
            return _DISPLAY.get(only, only.capitalize())
        return None

    if want == "cat":
        return _resolve(_CAT_ALIASES)  # cat names + nicknames
    else:
        return _resolve(_STATION_ALIASES)

def resolve_stations(text: str) -> List[str]:
    """
    Return unique canonical station display names found in text.
    Deterministic matching:
      1) Whole-word alias or key match
      2) Unambiguous prefix match (3–6 chars) across alias set
    Fuzzy matching lives upstream in the intent router.
    """
    t = f" {_norm(text)} "
    found: List[str] = []

    # 1) exact/alias hits first
    for key, aliases in _STATION_ALIASES.items():
        cands = [key] + list(aliases)
        for a in cands:
            a_norm = _norm(a)
            if a_norm and f" {a_norm} " in t:
                found.append(_DISPLAY.get(key, key.capitalize()))
                break

    # 2) unique prefix hits for unresolved keys
    already = set(found)
    tokens = set(tok for tok in _words(text) if tok not in STOPWORDS)
    for key, aliases in _STATION_ALIASES.items():
        disp = _DISPLAY.get(key, key.capitalize())
        if disp in already:
            continue
        cands = [key] + list(aliases)
        cand_tokens = [t for w in cands for t in _words(w) if t and t not in STOPWORDS]
        prefixes = { t[: max(3, min(len(t), 6)) ] for t in cand_tokens }
        hits = [tok for tok in tokens if any(tok.startswith(pfx) for pfx in prefixes)]
        if not hits:
            continue
        # ensure unambiguous
        ambiguous = False
        for other, other_aliases in _STATION_ALIASES.items():
            if other == key:
                continue
            other_cands = [other] + list(other_aliases)
            other_tokens = [t for w in other_cands for t in _words(w) if t and t not in STOPWORDS]
            other_pfx = { t[: max(3, min(len(t), 6)) ] for t in other_tokens }
            if any(tok.startswith(p) for tok in hits for p in other_pfx):
                ambiguous = True
                break
        if not ambiguous:
            found.append(disp)

    # dedupe preserving order
    seen = set()
    out: List[str] = []
    for name in found:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out
