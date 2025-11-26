# tomcat/aliases.py
"""Centralized alias + fuzzy matching helpers for cats and feeding stations."""

from __future__ import annotations
import csv
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
try:
    # Sheets + config available in this runtime; used for dynamic aliases
    from .config import settings  # type: ignore
    from .services.sheets_client import sheets_client  # type: ignore
except Exception:
    settings = None  # type: ignore
    sheets_client = None  # type: ignore

from .utils.fuzzy import best_match, fuzzy_ratio

# Module-level caches keep nickname lookups inexpensive across handler calls.

# One canonical place for both cat and station aliases.
# Populate these with data pulled from v5.6 config.js.
# All keys must be lowercase; values are canonical display strings.

# Canonical cat names; keep aliases minimal (self name plus normalized variants).
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

# ---- Dynamic aliases from Catabase (primary source), TTL-backed ----
_DYN_CAT_ALIASES: Dict[str, List[str]] = {}
_DYN_DISPLAY: Dict[str, str] = {}
_DYN_LAST_TS: float = 0.0
try:
    _DYN_TTL_SEC = int(getattr(settings, 'cat_aliases_ttl_sec', 60*60*2) or 7200)
except Exception:
    _DYN_TTL_SEC = 60 * 60 * 2  # default 2 hours

_FALLBACK_CAT_ALIAS_MAP: Dict[str, str] = {}
_FALLBACK_CAT_ALIAS_PAIRS: List[Tuple[str, str]] = []
_FALLBACK_CAT_MTIME: float = -1.0
_FALLBACK_CSV_PATHS: List[Path] = [Path("Catabase - CatDatabase.csv")]

def _parse_full_name_to_display(full: str) -> Optional[str]:
    if not full:
        return None
    # Expected like "115. Toothless" => "Toothless"
    m = re.match(r"\s*\d+[\.|\s]+(.+)$", str(full).strip())
    if m:
        return m.group(1).strip()
    return str(full).strip()

def _refresh_dyn_aliases(force: bool = False) -> None:
    global _DYN_CAT_ALIASES, _DYN_DISPLAY, _DYN_LAST_TS
    now = time.monotonic()
    if not force and (now - _DYN_LAST_TS) < _DYN_TTL_SEC:
        return
    new_aliases: Dict[str, List[str]] = {}
    new_display: Dict[str, str] = {}
    # Try Sheets first
    try:
        sid = getattr(settings, 'sheet_catabase_id', None) if settings else None
        if sid and sheets_client:
            ws = sheets_client().open_by_key(sid).worksheet("CatDatabase")
            rows = ws.get_all_values()
            data = rows[1:] if rows else []
            for r in data:
                full = (r[0] if r else '').strip()
                disp = _parse_full_name_to_display(full)
                if not disp:
                    continue
                key = disp.lower()
                vals: List[str] = []
                vals.extend(_alias_variants(disp))
                # include nickname variants from the sheet if present (column index 14 in our mapping)
                try:
                    nicks = (r[14] if len(r) > 14 else '').strip()
                except Exception:
                    nicks = ''
                if nicks:
                    for nick in re.split(r",|/|;|\n", nicks):
                        nick = nick.strip()
                        if not nick:
                            continue
                        vals.extend(_alias_variants(nick))
                        for tok in re.split(r"[^a-z0-9]+", nick.lower()):
                            if tok:
                                vals.extend(_alias_variants(tok))
                # unique preserve order
                seen = set(); out: List[str] = []
                for v in vals:
                    if v and v not in seen:
                        seen.add(v); out.append(v)
                new_aliases[key] = out
                new_display[key] = disp
            _DYN_CAT_ALIASES = new_aliases
            _DYN_DISPLAY = new_display
            _DYN_LAST_TS = now
            # Persist a lightweight CSV snapshot for offline fallback
            try:
                import csv as _csv
                with open("Catabase - CatDatabase.csv", "w", encoding="utf-8", newline="") as f:
                    w = _csv.writer(f)
                    # Write just Full Name + Common Nicknames if we have headers
                    w.writerow(["Full Name", "Common Nicknames"])
                    for key, disp in new_display.items():
                        # Rebuild nicknames approximation from aliases (not perfect but useful)
                        # Prefer original sheet nicks if we had them in r[14]; above we didn't keep per-row, so write blank.
                        w.writerow([disp, ""])
            except Exception:
                pass
            return
    except Exception:
        pass
    # Fallback: local CSV in repo if Sheets unavailable
    try:
        import csv
        path = "Catabase - CatDatabase.csv"
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                full = (row[0] if row else '').strip()
                disp = _parse_full_name_to_display(full)
                if not disp:
                    continue
                key = disp.lower()
                vals: List[str] = []
                vals.extend(_alias_variants(disp))
                # Guess nicknames column by header if present
                nicks = ''
                if header:
                    try:
                        idx = [h.strip().lower() for h in header].index('common nicknames')
                        nicks = (row[idx] if len(row) > idx else '').strip()
                    except Exception:
                        nicks = ''
                if nicks:
                    for nick in re.split(r",|/|;|\n", nicks):
                        nick = nick.strip()
                        if not nick:
                            continue
                        vals.extend(_alias_variants(nick))
                        for tok in re.split(r"[^a-z0-9]+", nick.lower()):
                            if tok:
                                vals.extend(_alias_variants(tok))
                seen = set(); out: List[str] = []
                for v in vals:
                    if v and v not in seen:
                        seen.add(v); out.append(v)
                new_aliases[key] = out
                new_display[key] = disp
        _DYN_CAT_ALIASES = new_aliases
        _DYN_DISPLAY = new_display
        _DYN_LAST_TS = now
    except Exception:
        # leave dynamic empty on failure
        _DYN_CAT_ALIASES = {}
        _DYN_DISPLAY = {}
        _DYN_LAST_TS = now


def _ensure_fallback_cat_aliases() -> None:
    """Populate fallback aliases from the local CSV when dynamic sources miss."""
    global _FALLBACK_CAT_ALIAS_MAP, _FALLBACK_CAT_ALIAS_PAIRS, _FALLBACK_CAT_MTIME
    for path in _FALLBACK_CSV_PATHS:
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            continue
        if _FALLBACK_CAT_ALIAS_MAP and _FALLBACK_CAT_MTIME == mtime:
            return
        alias_map: Dict[str, str] = {}
        with path.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if not row:
                    continue
                disp = _parse_full_name_to_display(row[0])
                if not disp:
                    continue
                variants = set(_alias_variants(disp))
                variants.add(_normalize(disp))
                for alias in variants:
                    alias_norm = (alias or "").strip().lower()
                    if not alias_norm:
                        continue
                    alias_map.setdefault(alias_norm, disp)
        if alias_map:
            _FALLBACK_CAT_ALIAS_MAP = alias_map
            _FALLBACK_CAT_ALIAS_PAIRS = [(alias, name) for alias, name in alias_map.items()]
            _FALLBACK_CAT_MTIME = mtime
            return
    # No CSV available; clear cache so future attempts retry
    _FALLBACK_CAT_ALIAS_MAP = {}
    _FALLBACK_CAT_ALIAS_PAIRS = []
    _FALLBACK_CAT_MTIME = -1.0


def _fallback_lookup_cat(text_norm: str, tokens: Iterable[str]) -> Optional[str]:
    """Attempt to resolve cat names using fallback CSV aliases when needed."""
    _ensure_fallback_cat_aliases()
    if not _FALLBACK_CAT_ALIAS_MAP:
        return None

    candidate_order: List[str] = []
    for tok in tokens:
        norm_tok = (tok or "").strip().lower()
        if norm_tok and norm_tok not in candidate_order:
            candidate_order.append(norm_tok)

    norm_full = (text_norm or "").strip().lower()
    if norm_full and norm_full not in candidate_order:
        candidate_order.append(norm_full)

    for cand in candidate_order:
        if cand in _FALLBACK_CAT_ALIAS_MAP:
            return _FALLBACK_CAT_ALIAS_MAP[cand]

    for cand in candidate_order:
        match = best_match(cand, _FALLBACK_CAT_ALIAS_PAIRS, threshold=70)
        if match:
            return match[0]

    return None

_STATION_ALIASES = {
    "west hall": ["west hall", "west", "hall"],
    "maintenance": ["maintenance", "maint"],
    "west campus": ["west campus"],
    "business": ["business", "coba"],
    "greens": ["the greens", "greens", "green", "grink", "grinks", "center chase", "center chase apartments", "center chase apartments & the greens"],
    "hop": ["hop", "pecan", "thwop", "thop", "heights", "hops", "heights on pecan"],
    "lot 50": ["lot 50", "lot50", "l50", "lot"],
    "mary kay and zen": ["mary kay and zen", "mkz", "zen", "mary kay", "mary", "kay", "zen gardens", "zen apartments", "mary kay apartments"],
    "mary kay & zen": ["mary kay & zen"],
    "microwave": ["microwave", "mike", "mikey", "miker", "micro", "wave", "old man", "michael", "him", "himb", "chemistry", "chemistry building", "chemistry/planetarium building", "planetarium", "planetarium building", "library", "life science building", "library life science building"],
    "snickers": ["snickers", "snicks"],
    "bookstore": ["First Baptist Church", "church", "first baptist", "bookstore"],
    "north campus": ["engineering research building", "erb", "north campus"],
    "centennial courts": ["centennial", "centennial courts"],
    "kc hall": ["kc hall", "kc", "kalpana chawla", "kalpana chawla hall"],
}

# Canonical display names (capitalization as you want to show)
_DISPLAY = {
    # Cats (subset will be overridden by alias_vocab() aggregation anyway)
    **{name.lower(): name for name in CAT_NAMES},
    # Stations
    "west hall": "West Hall",
    "maintenance": "Maintenance",
    "business": "Business",
    "greens": "Greens",
    "hop": "HOP",
    "lot 50": "Lot 50",
    "mary kay and zen": "Mary Kay and Zen",
    "mary kay & zen": "Mary Kay and Zen",
    "microwave": "Microwave",
    "snickers": "Snickers",
    "bookstore": "Bookstore",
    "north campus": "North Campus",
    "centennial courts": "Centennial Courts",
    "west campus": "West Campus",
    "kc hall": "KC Hall",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "so", "to", "for", "of", "in",
    "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "it", "its", "it's", "this", "that", "these", "those", "not",
    "no", "nor", "all", "any", "can", "could", "should", "would", "will", "just",
    "really", "very", "there", "here", "then", "than", "into", "over", "after",
    "before", "out", "up", "down", "again", "once", "why", "what", "who", "when",
    "where", "which", "while", "do", "does", "did", "have", "has", "had", "we",
    "us", "our", "ours", "you", "your", "yours", "i", "me", "my", "mine", "they",
    "them", "their", "theirs", "he", "him", "his", "she", "her", "hers", "lot",
    "hall", "station", "stations"
}


def alias_vocab() -> Dict[str, List[str]]:
    _refresh_dyn_aliases(force=False)
    cat_keys = set(_CAT_ALIASES.keys()) | set(_DYN_CAT_ALIASES.keys())
    station_keys = set(_STATION_ALIASES.keys())
    cats = sorted({ _display_for(k) for k in cat_keys })
    stations = sorted({ _display_for(k) for k in station_keys })
    all_names = sorted({ _display_for(k) for k in (cat_keys | station_keys) })
    return {"cats": cats, "stations": stations, "all": all_names}

def refresh_aliases_now() -> None:
    """Force a refresh of dynamic cat aliases from the sheet or CSV."""
    _refresh_dyn_aliases(force=True)

_WS = re.compile(r"\s+")
def norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower().strip())

def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower().strip())

def _words(s: str) -> List[str]:
    return [w for w in re.split(r"[^a-z0-9]+", _norm(s)) if w]

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _display_for(key: str) -> str:
    key_norm = (key or "").lower()
    if key_norm in _DYN_DISPLAY:
        return _DYN_DISPLAY[key_norm]
    return _DISPLAY.get(key_norm, key.title())


def _merged_cat_aliases() -> Dict[str, List[str]]:
    table = {k: list(v) for k, v in _CAT_ALIASES.items()}
    for key, aliases in _DYN_CAT_ALIASES.items():
        table[key] = list(aliases)
    return table


def _merged_station_aliases() -> Dict[str, List[str]]:
    return {k: list(v) for k, v in _STATION_ALIASES.items()}


def _alias_pairs(table: Dict[str, List[str]], include_stopword_aliases: bool = False) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for key, aliases in table.items():
        seen: set[str] = set()
        for alias in list(aliases) + [key]:
            alias_norm = _norm(alias)
            if not alias_norm:
                continue
            if alias_norm in STOPWORDS and not include_stopword_aliases:
                continue
            alias_tokens = [tok for tok in _words(alias) if tok]
            if alias_tokens and not include_stopword_aliases and all(tok in STOPWORDS for tok in alias_tokens):
                continue
            if alias_norm in seen:
                continue
            seen.add(alias_norm)
            pairs.append((alias_norm, key))
    return pairs


def _token_matches_alias(token: str, alias_token: str) -> bool:
    if not token or not alias_token:
        return False
    if token == alias_token:
        return True
    if len(token) >= 4 and alias_token.startswith(token):
        return True
    return False


def _resolve_exact_or_prefix(
    table: Dict[str, List[str]],
    text_norm: str,
    tokens: Iterable[str],
    include_stopword_aliases: bool = False,
) -> Optional[str]:
    for key, aliases in table.items():
        for alias in list(aliases) + [key]:
            alias_norm = _norm(alias)
            if not alias_norm:
                continue
            if alias_norm in STOPWORDS and not include_stopword_aliases:
                continue
            alias_tokens = [tok for tok in _words(alias) if tok]
            if alias_tokens and not include_stopword_aliases and all(tok in STOPWORDS for tok in alias_tokens):
                continue
            if re.search(rf"\b{re.escape(alias_norm)}\b", text_norm):
                return key

    key_tokens: Dict[str, List[str]] = {}
    for key, aliases in table.items():
        toks: List[str] = []
        for alias in list(aliases) + [key]:
            for tok in _words(alias):
                if not tok:
                    continue
                if tok in STOPWORDS and not include_stopword_aliases:
                    continue
                toks.append(tok)
        key_tokens[key] = list(dict.fromkeys(toks))

    hits: Dict[str, int] = {}
    for tok in tokens:
        if len(tok) < 3 or (tok in STOPWORDS and not include_stopword_aliases):
            continue
        matched = [key for key, toks in key_tokens.items() if any(_token_matches_alias(tok, t) for t in toks)]
        if len(matched) == 1:
            key = matched[0]
            hits[key] = hits.get(key, 0) + 1
    if len(hits) == 1:
        return next(iter(hits.keys()))
    return None


def resolve_station_or_cat(text: str, want: str, include_stopword_aliases: bool = False) -> Optional[str]:
    _refresh_dyn_aliases(force=False)
    text_norm = _normalize(text)
    if text_norm in STOPWORDS:
        return None
    raw_tokens = _words(text_norm)
    tokens = [tok for tok in raw_tokens if tok] if include_stopword_aliases else [tok for tok in raw_tokens if tok not in STOPWORDS]

    table = _merged_cat_aliases() if want == "cat" else _merged_station_aliases()
    key = _resolve_exact_or_prefix(table, text_norm, tokens, include_stopword_aliases=include_stopword_aliases)
    if key:
        return _display_for(key)

    alias_candidates = _alias_pairs(table, include_stopword_aliases=include_stopword_aliases)
    candidates = [text_norm] + tokens
    for cand in candidates:
        if not cand:
            continue
        if not include_stopword_aliases and cand in STOPWORDS:
            continue
        if len(cand) < 4 and not include_stopword_aliases:
            continue
        match = best_match(cand, alias_candidates, threshold=82)
        if match:
            return _display_for(match[0])

    if want == "cat":
        fallback = _fallback_lookup_cat(text_norm, tokens)
        if fallback:
            return fallback
    return None


def resolve_stations(text: str, *, include_stopword_aliases: bool = False) -> List[str]:
    _refresh_dyn_aliases(force=False)
    text_norm = _norm(text)
    raw_tokens = _words(text)
    tokens = [tok for tok in raw_tokens if tok] if include_stopword_aliases else [tok for tok in raw_tokens if tok not in STOPWORDS]
    table = _merged_station_aliases()

    found: List[str] = []
    found_keys: set[str] = set()
    padded = f" {text_norm} "

    for key, aliases in table.items():
        for alias in list(aliases) + [key]:
            alias_norm = _norm(alias)
            if not alias_norm:
                continue
            if alias_norm in STOPWORDS and not include_stopword_aliases:
                continue
            alias_tokens = [tok for tok in _words(alias) if tok]
            if alias_tokens and not include_stopword_aliases and all(tok in STOPWORDS for tok in alias_tokens):
                continue
            if f" {alias_norm} " in padded:
                if key not in found_keys:
                    found_keys.add(key)
                    found.append(_display_for(key))
                break

    for tok in tokens:
        if tok in STOPWORDS:
            continue
        disp = resolve_station_or_cat(tok, "station", include_stopword_aliases=include_stopword_aliases)
        if disp and disp not in found:
            found.append(disp)

    if not found:
        alias_candidates = _alias_pairs(table, include_stopword_aliases=include_stopword_aliases)
        for cand in tokens:
            if cand in STOPWORDS:
                continue
            if len(cand) < 4 and not include_stopword_aliases:
                continue
            if not cand:
                continue
            match = best_match(cand, alias_candidates, threshold=82)
            if match:
                disp = _display_for(match[0])
                if disp not in found:
                    found.append(disp)
    return found
