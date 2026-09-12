#tomcat/aliases.py
"""Centralized alias + fuzzy matching helpers for cats and feeding stations."""

from __future__ import annotations
import csv
import os
import re
import time
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from .stations import station_alias_table, station_display_for, station_generation
try:
    #Sheets + config available in this runtime; used for dynamic aliases
    from .config import settings  #type: ignore
    from .services.sheets_client import sheets_client  #type: ignore
except Exception:
    settings = None  #type: ignore
    sheets_client = None  #type: ignore

from .utils.fuzzy import FuzzyIndex, best_match, fuzzy_ratio

#Module-level caches keep nickname lookups inexpensive across handler calls.

#One canonical place for both cat and station aliases.
#Populate these with data pulled from v5.6 config.js.
#All keys must be lowercase; values are canonical display strings.

#Canonical cat names; keep aliases minimal (self name plus normalized variants).
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

# Optional nickname aliases that are not sourced from CatDatabase.
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
        #canonical name variants
        vals.extend(_alias_variants(disp))
        #nicknames and their token variants
        for nick in CAT_NICKNAMES.get(disp, []):
            vals.extend(_alias_variants(nick))
            #also split multi-words to allow partial tokens (e.g., "tito" from "Tito FluffyButt")
            for tok in re.split(r"[^a-z0-9]+", nick.lower()):
                if tok:
                    vals.extend(_alias_variants(tok))
        #unique preserve order
        seen = set(); out: List[str] = []
        for v in vals:
            if v not in seen:
                seen.add(v); out.append(v)
        table[key] = out
    return table

_CAT_ALIASES: Dict[str, List[str]] = _build_cat_aliases()

#---- Dynamic aliases from Catabase (primary source), TTL-backed ----
_DYN_CAT_ALIASES: Dict[str, List[str]] = {}
_DYN_DISPLAY: Dict[str, str] = {}
_DYN_LAST_TS: float = 0.0
#Bumped on every swap of the two dicts above so the derived indexes below know
#to rebuild. Everything else about alias matching is pure, so a generation token
#plus the station one is enough to cache the whole normalized table.
_DYN_GENERATION: int = 0


def _swap_dyn_aliases(aliases: Dict[str, List[str]], display: Dict[str, str]) -> None:
    global _DYN_CAT_ALIASES, _DYN_DISPLAY, _DYN_GENERATION
    _DYN_CAT_ALIASES = aliases
    _DYN_DISPLAY = display
    _DYN_GENERATION += 1


try:
    _DYN_TTL_SEC = int(getattr(settings, 'cat_aliases_ttl_sec', 60*60*2) or 7200)
except Exception:
    _DYN_TTL_SEC = 60 * 60 * 2  #default 2 hours

_FALLBACK_CAT_ALIAS_MAP: Dict[str, str] = {}
_FALLBACK_CAT_ALIAS_FUZZY: Optional[FuzzyIndex] = None
_FALLBACK_CAT_MTIME: float = -1.0
_CATABASE_CSV_PATH = Path("cache/catabase/Catabase - CatDatabase.csv")
_LEGACY_CATABASE_CSV_PATH = Path("Catabase - CatDatabase.csv")
_FALLBACK_CSV_PATHS: List[Path] = [_CATABASE_CSV_PATH, _LEGACY_CATABASE_CSV_PATH]

def _parse_full_name_to_display(full: str) -> Optional[str]:
    if not full:
        return None
    #Expected like "115. Toothless" => "Toothless"
    m = re.match(r"\s*\d+[\.|\s]+(.+)$", str(full).strip())
    if m:
        return m.group(1).strip()
    return str(full).strip()

_DYN_REFRESH_LOCK = threading.Lock()
_DYN_REFRESH_INFLIGHT = False


def _refresh_dyn_aliases(force: bool = False) -> None:
    """Refresh dynamic cat aliases without ever blocking the event loop.

    The real work (_do_dyn_alias_refresh) does a synchronous gspread
    get_all_values() over the network — on a slow / RAM-pressured host that can
    take several seconds. This is reached from the hot message path via
    resolve_station_or_cat(), so running it inline on the asyncio loop stalled
    the loop long enough to miss heartbeats and drop the Discord gateway
    (the recurring "online but silent" freeze). On TTL expiry we now kick a
    single background thread and return immediately; callers keep serving the
    existing in-memory cache + the local-CSV fallback in resolve_* until the
    refresh lands. force=True (admin recache) runs inline and MUST be called
    off-loop (see handlers/admin.py, which wraps it in asyncio.to_thread).
    """
    global _DYN_REFRESH_INFLIGHT, _DYN_LAST_TS
    now = time.monotonic()
    if not force and (now - _DYN_LAST_TS) < _DYN_TTL_SEC:
        return
    if force:
        _do_dyn_alias_refresh()
        return
    with _DYN_REFRESH_LOCK:
        if _DYN_REFRESH_INFLIGHT:
            return
        _DYN_REFRESH_INFLIGHT = True
        # Optimistically bump the timestamp so we don't re-kick a thread on
        # every message while the fetch is in flight.
        _DYN_LAST_TS = now

    def _runner() -> None:
        global _DYN_REFRESH_INFLIGHT
        try:
            _do_dyn_alias_refresh()
        finally:
            with _DYN_REFRESH_LOCK:
                _DYN_REFRESH_INFLIGHT = False

    threading.Thread(target=_runner, name="dyn-alias-refresh", daemon=True).start()


def _do_dyn_alias_refresh() -> None:
    """Blocking refresh of dynamic cat aliases from Sheets (CSV fallback).

    MUST run off the event loop — either on the background thread started by
    _refresh_dyn_aliases, or from a non-async caller.
    """
    global _DYN_LAST_TS
    now = time.monotonic()
    new_aliases: Dict[str, List[str]] = {}
    new_display: Dict[str, str] = {}
    #Try Sheets first
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
                #include nickname variants from the sheet if present (column index 14 in our mapping)
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
                #unique preserve order
                seen = set(); out: List[str] = []
                for v in vals:
                    if v and v not in seen:
                        seen.add(v); out.append(v)
                new_aliases[key] = out
                new_display[key] = disp
            _swap_dyn_aliases(new_aliases, new_display)
            _DYN_LAST_TS = now
            #Persist a lightweight CSV snapshot for offline fallback
            try:
                import csv as _csv
                _CATABASE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
                with _CATABASE_CSV_PATH.open("w", encoding="utf-8", newline="") as f:
                    w = _csv.writer(f)
                    #Write just Full Name + Common Nicknames if we have headers
                    w.writerow(["Full Name", "Common Nicknames"])
                    for key, disp in new_display.items():
                        #Rebuild nicknames approximation from aliases (not perfect but useful)
                        #Prefer original sheet nicks if we had them in r[14]; above we didn't keep per-row, so write blank.
                        w.writerow([disp, ""])
            except Exception:
                pass
            return
    except Exception:
        pass
    #Fallback: local CSV in repo if Sheets unavailable
    try:
        import csv
        for path in _FALLBACK_CSV_PATHS:
            if not path.exists():
                continue
            with path.open('r', encoding='utf-8') as f:
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
                    #Guess nicknames column by header if present
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
            _swap_dyn_aliases(new_aliases, new_display)
            _DYN_LAST_TS = now
            return
    except Exception:
        #leave dynamic empty on failure
        _swap_dyn_aliases({}, {})
        _DYN_LAST_TS = now


def _ensure_fallback_cat_aliases() -> None:
    """Populate fallback aliases from the local CSV when dynamic sources miss."""
    global _FALLBACK_CAT_ALIAS_MAP, _FALLBACK_CAT_ALIAS_FUZZY, _FALLBACK_CAT_MTIME
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
            _FALLBACK_CAT_ALIAS_FUZZY = FuzzyIndex(alias_map.items())
            _FALLBACK_CAT_MTIME = mtime
            return
    #No CSV available; clear cache so future attempts retry
    _FALLBACK_CAT_ALIAS_MAP = {}
    _FALLBACK_CAT_ALIAS_FUZZY = None
    _FALLBACK_CAT_MTIME = -1.0


def _fallback_lookup_cat(text_norm: str, tokens: Iterable[str]) -> Optional[str]:
    """Attempt to resolve cat names using fallback CSV aliases when needed."""
    _ensure_fallback_cat_aliases()
    if not _FALLBACK_CAT_ALIAS_MAP or _FALLBACK_CAT_ALIAS_FUZZY is None:
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
        match = _FALLBACK_CAT_ALIAS_FUZZY.best(cand, threshold=70)
        if match:
            return match[0]

    return None

# Canonical display names used when no dynamic alias source overrides them.
_DISPLAY = {
    #Cats (subset will be overridden by alias_vocab() aggregation anyway)
    **{name.lower(): name for name in CAT_NAMES},
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


_VOCAB_CACHE: Optional[Tuple[Tuple[int, int], Dict[str, List[str]]]] = None


def alias_vocab() -> Dict[str, List[str]]:
    """Display names by category. Cached: the router asks for it per message."""
    global _VOCAB_CACHE
    _refresh_dyn_aliases(force=False)
    generation = (_DYN_GENERATION, station_generation())
    if _VOCAB_CACHE is not None and _VOCAB_CACHE[0] == generation:
        return _VOCAB_CACHE[1]
    cat_keys = set(_CAT_ALIASES) | set(_DYN_CAT_ALIASES)
    station_keys = set(station_alias_table())
    vocab = {
        "cats": sorted({_display_for(k) for k in cat_keys}),
        "stations": sorted({_display_for(k) for k in station_keys}),
        "all": sorted({_display_for(k) for k in (cat_keys | station_keys)}),
    }
    _VOCAB_CACHE = (generation, vocab)
    return vocab

_NAME_SCAN_CACHE: Dict[str, Tuple[Tuple[int, int], Optional["re.Pattern[str]"], List[Tuple["re.Pattern[str]", str]]]] = {}


def display_names_in(text: str, want: str) -> List[str]:
    """Display names of `want` that appear as whole words in the text.

    Catches bare mentions such as "Twix" that the alias resolver skips because
    it only ever returns one name. Patterns are compiled once per alias-table
    change: building ~170 of them per call was the cost of this scan.
    """
    lowered = (text or "").lower()
    if not lowered:
        return []
    generation = (_DYN_GENERATION, station_generation())
    cached = _NAME_SCAN_CACHE.get(want)
    if cached is None or cached[0] != generation:
        names = alias_vocab().get(f"{want}s", [])
        patterns = [(re.compile(_boundary(name.lower())), name) for name in names]
        gate = re.compile("|".join(_boundary(n.lower()) for n in names)) if names else None
        cached = (generation, gate, patterns)
        _NAME_SCAN_CACHE[want] = cached
    _generation, gate, patterns = cached
    if gate is None or not gate.search(lowered):
        return []
    return [name for pattern, name in patterns if pattern.search(lowered)]


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
    station_disp = station_display_for(key_norm)
    if station_disp:
        return station_disp
    return _DISPLAY.get(key_norm, key.title())


def _merged_cat_aliases() -> Dict[str, List[str]]:
    table = {k: list(v) for k, v in _CAT_ALIASES.items()}
    for key, aliases in _DYN_CAT_ALIASES.items():
        table[key] = list(aliases)
    return table


def _merged_station_aliases() -> Dict[str, List[str]]:
    return station_alias_table()


class _AliasIndex:
    """Everything alias matching needs, normalized once per alias-table change.

    Matching used to re-normalize all ~200 aliases, rebuild the per-key token
    map, and interpolate a fresh word-boundary pattern for each alias on *every*
    call - and resolve_stations() calls into it once per word. That was several
    milliseconds of synchronous work per Discord message. The tables only change
    when the Catabase refresh or the stations file lands, so index them once.

    exact       alias -> key, first definition wins (the old scan order)
    phrases     (compiled word-boundary pattern, key), most specific alias first
    any_phrase  every alias in one pattern, used as a cheap gate
    token_keys  word, or >=4-char prefix of one, -> keys that own it
    key_padded  key -> its aliases padded with spaces, for substring tests
    pairs       (alias, key) for fuzzy matching
    fuzzy       the same pairs, pre-split for repeated FuzzyIndex queries
    """

    __slots__ = ("exact", "phrases", "any_phrase", "token_keys", "key_padded", "pairs", "fuzzy")

    def __init__(self, table: Dict[str, List[str]], include_stopword_aliases: bool):
        self.exact: Dict[str, str] = {}
        self.pairs: List[Tuple[str, str]] = []
        self.token_keys: Dict[str, List[str]] = {}
        self.key_padded: List[Tuple[str, Tuple[str, ...]]] = []
        scored: List[Tuple[Tuple[int, int], str, str]] = []

        for key, aliases in table.items():
            seen: set[str] = set()
            padded: List[str] = []
            for alias in list(aliases) + [key]:
                alias_norm = _norm(alias)
                if not alias_norm or alias_norm in seen:
                    continue
                if alias_norm in STOPWORDS and not include_stopword_aliases:
                    continue
                alias_tokens = _words(alias_norm)
                if alias_tokens and not include_stopword_aliases and all(
                    tok in STOPWORDS for tok in alias_tokens
                ):
                    continue
                seen.add(alias_norm)
                self.exact.setdefault(alias_norm, key)
                self.pairs.append((alias_norm, key))
                scored.append(((len(alias_norm), len(alias_tokens)), alias_norm, key))
                padded.append(f" {alias_norm} ")
                for tok in alias_tokens:
                    if tok in STOPWORDS and not include_stopword_aliases:
                        continue
                    #Exact hits work at any length; prefix hits need >=4 chars.
                    for lookup in (tok, *(tok[:n] for n in range(4, len(tok)))):
                        owners = self.token_keys.setdefault(lookup, [])
                        if key not in owners:
                            owners.append(key)
            self.key_padded.append((key, tuple(padded)))

        #Stable sort keeps table order among equally specific aliases, so the
        #first match found below is the one the old max-score scan picked.
        scored.sort(key=lambda item: item[0], reverse=True)
        self.phrases: List[Tuple["re.Pattern[str]", str]] = [
            (re.compile(_boundary(alias)), key) for _score, alias, key in scored
        ]
        self.any_phrase = (
            re.compile("|".join(_boundary(alias) for _s, alias, _k in scored))
            if scored else None
        )
        self.fuzzy = FuzzyIndex(self.pairs)

    def resolve_exact_or_prefix(self, text_norm: str, tokens: Iterable[str]) -> Optional[str]:
        """Longest alias phrase in the text, else a token that names one key."""
        key = self.exact.get(text_norm)
        if key is not None:
            return key
        #One combined pattern rules out the (common) no-alias case before the
        #per-alias walk that works out *which* alias matched.
        if self.any_phrase is not None and self.any_phrase.search(text_norm):
            for pattern, key in self.phrases:
                if pattern.search(text_norm):
                    return key

        hits: set[str] = set()
        for tok in tokens:
            if len(tok) < 3:
                continue
            owners = self.token_keys.get(tok)
            if owners is not None and len(owners) == 1:
                hits.add(owners[0])
                if len(hits) > 1:
                    return None
        return next(iter(hits)) if len(hits) == 1 else None


def _boundary(alias: str) -> str:
    return r"\b" + re.escape(alias) + r"\b"


_INDEX_CACHE: Dict[Tuple[str, bool], Tuple[Tuple[int, int], _AliasIndex]] = {}


def _alias_index(want: str, include_stopword_aliases: bool = False) -> _AliasIndex:
    generation = (_DYN_GENERATION, station_generation())
    ckey = (want, include_stopword_aliases)
    cached = _INDEX_CACHE.get(ckey)
    if cached is not None and cached[0] == generation:
        return cached[1]
    table = _merged_cat_aliases() if want == "cat" else _merged_station_aliases()
    index = _AliasIndex(table, include_stopword_aliases)
    _INDEX_CACHE[ckey] = (generation, index)
    return index


def _lookup_tokens(text_norm: str, include_stopword_aliases: bool) -> List[str]:
    tokens = _words(text_norm)
    if include_stopword_aliases:
        return tokens
    return [tok for tok in tokens if tok not in STOPWORDS]


#Resolution is pure for a given alias table, and resolve_stations() asks about
#every word of a message, so repeated words (and repeated messages) get answered
#from here. Keyed on the normalized text so "West Hall" and "west  hall" share
#an entry; dropped wholesale whenever any alias source changes.
_RESOLVE_CACHE: Dict[Tuple[str, str, bool], Optional[str]] = {}
_RESOLVE_CACHE_GENERATION: Optional[Tuple[int, int, float]] = None
_RESOLVE_CACHE_MAX = 4096


def resolve_station_or_cat(text: str, want: str, include_stopword_aliases: bool = False) -> Optional[str]:
    global _RESOLVE_CACHE_GENERATION
    _refresh_dyn_aliases(force=False)
    text_norm = _normalize(text)
    if text_norm in STOPWORDS:
        return None

    generation = (_DYN_GENERATION, station_generation(), _FALLBACK_CAT_MTIME)
    if generation != _RESOLVE_CACHE_GENERATION:
        _RESOLVE_CACHE.clear()
        _RESOLVE_CACHE_GENERATION = generation
    ckey = (text_norm, want, include_stopword_aliases)
    if ckey in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[ckey]

    found = _resolve_uncached(text_norm, want, include_stopword_aliases)
    if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
        _RESOLVE_CACHE.clear()
    _RESOLVE_CACHE[ckey] = found
    return found


def _resolve_uncached(text_norm: str, want: str, include_stopword_aliases: bool) -> Optional[str]:
    tokens = _lookup_tokens(text_norm, include_stopword_aliases)

    index = _alias_index(want, include_stopword_aliases)
    key = index.resolve_exact_or_prefix(text_norm, tokens)
    if key:
        return _display_for(key)

    #A one-word query yields the same candidate twice; fuzzy scoring the whole
    #alias table is the expensive part, so ask about each spelling once.
    for cand in dict.fromkeys([text_norm, *tokens]):
        if not cand:
            continue
        if not include_stopword_aliases and (cand in STOPWORDS or len(cand) < 4):
            continue
        match = index.fuzzy.best(cand, threshold=82)
        if match:
            return _display_for(match[0])

    if want == "cat":
        return _fallback_lookup_cat(text_norm, tokens) or None
    return None


def resolve_stations(text: str, *, include_stopword_aliases: bool = False) -> List[str]:
    _refresh_dyn_aliases(force=False)
    text_norm = _norm(text)
    tokens = _lookup_tokens(text_norm, include_stopword_aliases)
    index = _alias_index("station", include_stopword_aliases)

    found: List[str] = []
    padded = f" {text_norm} "
    for key, aliases in index.key_padded:
        if any(alias in padded for alias in aliases):
            found.append(_display_for(key))

    for tok in tokens:
        if tok in STOPWORDS:
            continue
        disp = resolve_station_or_cat(tok, "station", include_stopword_aliases=include_stopword_aliases)
        if disp and disp not in found:
            found.append(disp)

    if not found:
        for cand in tokens:
            if cand in STOPWORDS or (len(cand) < 4 and not include_stopword_aliases):
                continue
            match = index.fuzzy.best(cand, threshold=82)
            if match:
                disp = _display_for(match[0])
                if disp not in found:
                    found.append(disp)
    return found
