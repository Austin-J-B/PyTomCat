"""Dues ingestion + Gmail logging pipeline for CCC membership tracking."""

from __future__ import annotations
import os
import re
import csv
import asyncio
import json
import discord
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
try:
    from zoneinfo import ZoneInfo  #py>=3.9
except Exception:
    ZoneInfo = None  #type: ignore
from typing import Any, Dict, Optional, List, Tuple

from ..logger import log_event, log_action
from ..config import settings
from ..utils.permissions import is_officer, officer_role_ids
from ..utils.payments import detect_provider
from . import finance

try:
    from ..utils.sender import safe_send
except Exception:
    async def safe_send(ch, text, **kwargs):
        await ch.send(text, **kwargs)

from .gmail import (
    EMAILS_DIR,
    _EMAIL_LOG_LOCK,
    _build_gmail_service,
    _log_emails_batch,
)


#============================
#Time helpers
#============================

_DUES_SCHEDULER_LOCK = asyncio.Lock()
_DUES_SCHEDULER_STARTED = False
_LAST_DUES_RUN_KEY: Optional[str] = None
_DUES_LOCK_DIR = Path("logs") / "dues" / "locks"

def _dues_now() -> datetime:
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(getattr(settings, "timezone", "America/Chicago"))
        except Exception:
            tz = None
    return datetime.now(tz) if tz else datetime.now()

def _dues_run_key(now: Optional[datetime] = None) -> str:
    now = now or _dues_now()
    return now.date().isoformat()

def _dues_lock_path(key: str) -> Path:
    safe = re.sub(r"[^0-9\-]", "", key)
    return _DUES_LOCK_DIR / f"{safe}.lock"

def _acquire_dues_lock(key: str) -> bool:
    """Best-effort cross-process guard to prevent duplicate daily runs."""
    try:
        _DUES_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = _dues_lock_path(key)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            payload = f"pid={os.getpid()} started_at={_now_iso()}".encode("utf-8")
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        #If locking fails unexpectedly, fall back to in-process guard
        return True

def _now_iso() -> str:
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(getattr(settings, "timezone", "America/Chicago"))
        except Exception:
            tz = None
    now = datetime.now(tz) if tz else datetime.now()
    return now.isoformat()

#============================
# Dues analysis helpers
#============================

DUES_DIR = os.path.join("logs", "dues")
os.makedirs(DUES_DIR, exist_ok=True)
DUES_INDEX = os.path.join(DUES_DIR, "index.jsonl")
_DUES_PROCESSED_EMOJI = os.getenv("DUES_PROCESSED_EMOJI", "✅")
_DUES_INDEX_CACHE: Optional[set[str]] = None
_DUES_INDEX_TS: float = 0.0

#----------- Normalization & Regexes -----------
_PROVIDER_RE = re.compile(
    r"\b(paypal|venmo(?:ed)?|cash\s?app(?:ed|d)?|cashapp(?:ed|d)?|cash-app(?:ed|d)?|zelle(?:d|'d)?|in\s*person|cash|irl)\b",
    re.I,
)
_AMOUNT_RE = re.compile(r"(?<!\d)\$?\s*(\d{1,6}(?:\.\d{2})?)(?!\d)")
_MONEY_RE = re.compile(r"-?\d+(?:\.\d+)?")
_VENMO_HANDLE_RE = re.compile(r"@[A-Za-z0-9._-]{2,32}")
_CASHAPP_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9_]{0,19}")
_PAID_PHRASE_RE = re.compile(r"\b(paid (?:on|via|through|to)|sent you|paid you|i paid|i just paid)\b", re.I)
_DUES_WORD_RE = re.compile(r"\bdues?\b", re.I)
_DONATION_WORD_RE = re.compile(r"\bdonat(?:e|ion|ed)\b", re.I)

try:
    from rapidfuzz import fuzz as rf_fuzz
    def _ratio(a: str, b: str) -> int:
        try:
            return int(rf_fuzz.token_set_ratio(a, b))
        except Exception:
            return 0
except Exception:
    import difflib
    def _ratio(a: str, b: str) -> int:
        return int(100 * difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio())

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _norm_user(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def _simplify_name(s: str) -> str:
    if not s:
        return ""
    s2 = re.sub(r"\([^)]*\)", " ", s)
    s2 = re.sub(r"[^A-Za-z'`\-\s]", " ", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2

def _name_match(a: str, b: str) -> int:
    return _ratio(_simplify_name(a), _simplify_name(b))

#--- Jaccard for token overlap ---
def _jaccard_tokens(a: str, b: str) -> float:
    a = (a or "").lower().strip()
    b = (b or "").lower().strip()
    if not a or not b:
        return 0.0
    A = set(re.findall(r"[a-z0-9]+", a))
    B = set(re.findall(r"[a-z0-9]+", b))
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, len(A | B))

#--- Provider normalize ---
_DEF_PROVIDER_MAP = {
    "cash app": "cashapp",
    "cashapp": "cashapp",
    "cashappd": "cashapp",
    "cashapped": "cashapp",
    "cash-app": "cashapp",
    "cash-appd": "cashapp",
    "cash-apped": "cashapp",
    "paypal": "paypal",
    "venmo": "venmo",
    "venmoed": "venmo",
    "zelle": "zelle",
    "zelled": "zelle",
    "zelle'd": "zelle",
    "inperson": "cash",
    "cash": "cash",
}

def _norm_provider(text: str) -> str:
    t = (text or "").lower()
    for k, v in _DEF_PROVIDER_MAP.items():
        if k in t:
            return v
    return ""

_AMOUNT_POSITIVE_HINTS = (
    "paid",
    "sent",
    "payment",
    "donation",
    "donated",
    "dues",
    "membership",
    "received",
    "transfer",
    "credit",
    "contribution",
    "support",
    "venmo",
    "paypal",
    "cashapp",
    "cash app",
    "zelle",
    "zelled",
)
_AMOUNT_NEGATIVE_HINTS = (
    "spent",
    "statement",
    "invoice",
    "receipt",
    "charge",
    "order",
)


def _amount_candidates(text: str) -> list[float]:
    vals: list[float] = []
    if not text:
        return vals
    lower = text.lower()
    for match in _AMOUNT_RE.finditer(text):
        num_start, num_end = match.span(1)
        prefix = text[max(0, num_start - 3):num_start]
        suffix = text[num_end:min(len(text), num_end + 3)]
        has_dollar = ('$' in prefix) or ('$' in suffix)
        context = lower[max(0, num_start - 20):min(len(text), num_end + 20)]
        has_positive = any(hint in context for hint in _AMOUNT_POSITIVE_HINTS)
        has_negative = any(hint in context for hint in _AMOUNT_NEGATIVE_HINTS)
        if not (has_dollar or has_positive):
            continue
        if has_negative and not has_positive:
            continue
        if not has_dollar:
            prev_char = ''
            j = num_start - 1
            while j >= 0 and text[j].isspace():
                j -= 1
            if j >= 0:
                prev_char = text[j]
            next_char = ''
            k = num_end
            while k < len(text) and text[k].isspace():
                k += 1
            if k < len(text):
                next_char = text[k]
            #Skip numbers that are part of URL segments or alphanumeric ids
            if prev_char and prev_char.isalpha():
                continue
            if next_char and next_char.isalpha():
                continue
            if prev_char in {'/', '#', '@'}:
                continue
        try:
            val = float(match.group(1))
            if not has_dollar and val > float(getattr(settings, 'dues_max_unlabeled_amount', 200.0) or 200.0):
                continue
            vals.append(val)
        except Exception:
            continue
    #De-duplicate while preserving order
    seen = set()
    ordered: list[float] = []
    for v in vals:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered

#--- Portal parsing & filter ---

def _handles_from_text(text: str) -> list[str]:
    ven = _VENMO_HANDLE_RE.findall(text or '')
    cas = _CASHAPP_RE.findall(text or '')
    out = []
    for h in ven + cas:
        t = h.lstrip('@$')
        if not t:
            continue
        out.append(t)
    return out


def _parse_portal_message(msg) -> Dict[str, Any]:
    text = _norm_space(getattr(msg, 'content', '') or '')
    provider = None
    m = _PROVIDER_RE.search(text)
    if m:
        p = m.group(1).lower().replace(' ', '')
        if 'cash' in p and 'app' in m.group(1).lower():
            provider = 'cashapp'
        elif p == 'irl':
            provider = 'cash'
        else:
            provider = p
    amounts = _amount_candidates(text)
    venmo_handles = _VENMO_HANDLE_RE.findall(text)
    cash_handles = _CASHAPP_RE.findall(text)

    # Extract the payer name from parentheses first, then fall back to text patterns.
    name = None
    paren = re.search(r"\(([^(]{1,60})\)", text)
    if paren:
        inner = paren.group(1).strip()
        if not (_VENMO_HANDLE_RE.search(inner) or _CASHAPP_RE.search(inner)):
            name = inner
    if not name:
        m2 = _PAID_PHRASE_RE.search(text)
        if m2:
            tail = text[m2.end():].strip()
            cand = re.match(r"([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})", tail)
            if cand:
                name = cand.group(1).strip()
    if not name:
        mlead = re.match(r"^\s*([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})\s+paid\b", text)
        if mlead:
            name = mlead.group(1).strip()
    if not name and ',' in text:
        head = text.split(',', 1)[0].strip()
        if re.match(r"[A-Za-z].+\s+[A-Za-z].+", head):
            name = head
    if name and 'http' in name.lower():
        name = None

    author_obj = getattr(msg, 'author', None)
    author_username = getattr(author_obj, 'name', '')
    author_display = getattr(author_obj, 'display_name', '') or author_username

    return {
        "author_id": int(getattr(author_obj,'id', 0) or 0),
        "author_name": author_username,
        "author_display": author_display,
        "ts": getattr(msg, 'created_at', None) or datetime.now(timezone.utc),
        "content": text,
        "provider": provider,
        "amounts": amounts,
        "handles": venmo_handles + cash_handles,
        "name": name,
    }


def _is_explicit_payment_message(text: str) -> bool:
    t = " " + (text or '').lower() + " "
    provider = any(k in t for k in [
        "venmo", "paypal", "cash app", "cashapp", "cash-app", "in person", "cash", "zelle", "zelled"
    ])
    if not provider:
        return False
    #In the dues portal context, mentioning a provider is sufficient evidence.
    #The portal is specifically for dues payments, so "[Name] [Provider]" is valid.
    #
    #Still check for strong signals first (paid, sent, etc.) for clarity,
    #but if a provider is mentioned, we accept it.
    if any(p in t for p in [" paid ", " paid,", " paid.", " paid!", " sent ", " sent.", " sent,", " donated "]):
        return True
    #Common phrasings seen in payment emails, e.g., "used cashapp"
    if " used " in t:
        return True
    #Provider-verbed variants: "venmoed", "cashapped", "zelle'd"
    if any(v in t for v in ["venmoed", "cashapped", "cashapp'd", "zelle'd", "zelled"]):
        return True
    #"via/through <provider>"
    if (" via " in t or " through " in t):
        return True
    # In the dues portal, a provider name alone is enough because the channel
    # already establishes payment context. This covers formats such as
    # "Charlotte Brownlee PayPal" or "John Smith Venmo".
    return True

#--- Sheet ingress (via Sheets API) ---

_MEMBERSHIP_ROWS_CACHE: Optional[list[dict]] = None
_MEMBERSHIP_ROWS_TS: float = 0.0
_MEMBERSHIP_ROWS_LAST_SOURCE: str = 'uninitialized'
_MEMBERSHIP_ROWS_LAST_ERROR: str = ''
_MEMBERSHIP_ROWS_LAST_AUTHORITATIVE: bool = False

def _set_membership_load_state(source: str, *, error: str = '', authoritative: bool = False) -> None:
    global _MEMBERSHIP_ROWS_LAST_SOURCE, _MEMBERSHIP_ROWS_LAST_ERROR, _MEMBERSHIP_ROWS_LAST_AUTHORITATIVE
    _MEMBERSHIP_ROWS_LAST_SOURCE = str(source or 'unknown')
    _MEMBERSHIP_ROWS_LAST_ERROR = str(error or '')
    _MEMBERSHIP_ROWS_LAST_AUTHORITATIVE = bool(authoritative)

def _membership_snapshot_paths() -> list[Path]:
    candidates = [
        Path('CCC megasheet - Membership Application List.csv'),
        Path('Membership Application List.csv'),
    ]
    try:
        candidates.extend(sorted(Path('.').glob('*Membership Application List*.csv')))
    except Exception:
        pass
    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out

def _parse_membership_table(rows: list[list[str]]) -> list[dict]:
    if not rows:
        return []
    def hkey(s: str) -> str:
        return re.sub(r"[^a-z]+", "", (s or '').lower())
    target_keys = {
        'fullname','fulllegalname','legalname','name',
        'discordusername','discordhandle','discord','discordname','discordtag','discordid',
        'paymentusername','paymenthandle','payhandle','paymentuser','paymenttag',
        'paidwhere','paidvia','provider','method','wherepaid',
        'duesordonation','duesdonation','type','reason','category','donation','donations',
        'verified','isverified','email','semester'
    }
    header_idx = 0
    best_hits = -1
    sample_limit = min(len(rows), 30)
    for i in range(sample_limit):
        row = rows[i]
        keys = {hkey(c) for c in row if c}
        hits = len(keys & target_keys)
        if hits > best_hits and hits >= 2:
            best_hits = hits
            header_idx = i
    header = rows[header_idx]
    data = rows[header_idx+1:]
    log_action('dues_membership_header', f'row={header_idx}', '|'.join(header[:12]))
    idx = {hkey(h): i for i, h in enumerate(header)}
    def col(name_keys: List[str]) -> int:
        for k in name_keys:
            if k in idx:
                return idx[k]
        return -1
    i_date = col(['date','timestamp','submittedat'])
    i_full = col(['fullname','fulllegalname','legalname','name'])
    i_disc = col(['discordusername','discordhandle','discord','discordname','discordtag','discordid'])
    i_payu = col(['paymentusername','paymenthandle','payhandle','paymentuser','paymenttag'])
    i_where= col(['paidwhere','paidvia','provider','method','wherepaid'])
    i_kind = col(['duesordonation','duesdonation','type','reason','category'])
    i_email= col(['email'])
    i_sem  = col(['semester'])
    i_ver  = col(['verified','isverified'])
    i_inv  = col(['mavorgsinvite','invite','mavorgs'])
    i_don  = col(['donation','donations','donationamount','donation?'])
    out = []
    for r in data:
        def get(i):
            if i < 0 or i >= len(r):
                return ''
            val = r[i]
            if isinstance(val, str):
                return val.strip()
            if val is None:
                return ''
            return str(val).strip()
        def _truthy(s: str) -> bool:
            v = (s or '').strip().lower()
            return v in {'true','yes','y','1','paid','verified','done','ok','x','âœ…'}
        row = {
            'date': get(i_date),
            'full_name': get(i_full),
            'discord_username': get(i_disc),
            'payment_username': get(i_payu),
            'paid_where': get(i_where),
            'kind': get(i_kind),
            'email': get(i_email),
            'semester': get(i_sem),
            'verified': _truthy(get(i_ver)) if i_ver >= 0 else False,
            'mavorgs_invite': _truthy(get(i_inv)) if i_inv >= 0 else False,
            'donation_amount': get(i_don),
        }
        if any(bool(v) for v in row.values()):
            out.append(row)
    log_action('dues_membership_rows', f'total={len(rows)-1}', f'usable={len(out)}')
    return out

def _load_membership_rows_from_csv(path: Path) -> list[dict]:
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        rows = [list(row) for row in csv.reader(f)]
    return _parse_membership_table(rows)

def _load_membership_rows():
    try:
        sid = getattr(settings, 'sheet_megasheet_id', None)
        if not sid:
            raise RuntimeError('missing_sheet_id')
        from ..services.sheets_client import sheets_client as _sc
        ws_name = getattr(settings, 'membership_ws_title', 'Membership Application List')
        log_action('dues_membership_open', f'sheet={sid}', f'ws={ws_name}')
        # Use a small TTL to avoid 429s when the command is run back-to-back.
        global _MEMBERSHIP_ROWS_CACHE, _MEMBERSHIP_ROWS_TS
        import time as _time
        ttl = max(1, int(getattr(settings, 'dues_membership_ttl_sec', 300) or 300))
        if _MEMBERSHIP_ROWS_CACHE is not None and (_time.monotonic() - _MEMBERSHIP_ROWS_TS) < ttl:
            _set_membership_load_state('cache_ttl', authoritative=True)
            return list(_MEMBERSHIP_ROWS_CACHE)
        ws = _sc().open_by_key(sid).worksheet(ws_name)
        rows = ws.get_all_values()
        if not rows:
            _set_membership_load_state('sheets', authoritative=True)
            return []
        def hkey(s: str) -> str:
            return re.sub(r"[^a-z]+", "", (s or '').lower())
        target_keys = {
            'fullname','fulllegalname','legalname','name',
            'discordusername','discordhandle','discord','discordname','discordtag','discordid',
            'paymentusername','paymenthandle','payhandle','paymentuser','paymenttag',
            'paidwhere','paidvia','provider','method','wherepaid',
            'duesordonation','duesdonation','type','reason','category','donation','donations',
            'verified','isverified','email','semester'
        }
        header_idx = 0
        best_hits = -1
        sample_limit = min(len(rows), 30)
        for i in range(sample_limit):
            row = rows[i]
            keys = {hkey(c) for c in row if c}
            hits = len(keys & target_keys)
            if hits > best_hits and hits >= 2:
                best_hits = hits
                header_idx = i
        header = rows[header_idx]
        data = rows[header_idx+1:]
        log_action('dues_membership_header', f'row={header_idx}', '|'.join(header[:12]))
        idx = {hkey(h): i for i, h in enumerate(header)}
        def col(name_keys: List[str]) -> int:
            for k in name_keys:
                if k in idx: return idx[k]
            return -1
        i_date = col(['date','timestamp','submittedat'])
        i_full = col(['fullname','fulllegalname','legalname','name'])
        i_disc = col(['discordusername','discordhandle','discord','discordname','discordtag','discordid'])
        i_payu = col(['paymentusername','paymenthandle','payhandle','paymentuser','paymenttag'])
        i_where= col(['paidwhere','paidvia','provider','method','wherepaid'])
        i_kind = col(['duesordonation','duesdonation','type','reason','category'])
        i_email= col(['email'])
        i_sem  = col(['semester'])
        i_ver  = col(['verified','isverified'])
        i_inv  = col(['mavorgsinvite','invite','mavorgs'])
        i_don  = col(['donation','donations','donationamount','donation?'])
        out = []
        for r in data:
            def get(i):
                if i < 0 or i >= len(r):
                    return ''
                val = r[i]
                if isinstance(val, str):
                    return val.strip()
                if val is None:
                    return ''
                return str(val).strip()
            def _truthy(s: str) -> bool:
                v = (s or '').strip().lower()
                return v in {'true','yes','y','1','paid','verified','done','ok','x','✅'}
            row = {
                'date': get(i_date),
                'full_name': get(i_full),
                'discord_username': get(i_disc),
                'payment_username': get(i_payu),
                'paid_where': get(i_where),
                'kind': get(i_kind),
                'email': get(i_email),
                'semester': get(i_sem),
                'verified': _truthy(get(i_ver)) if i_ver >= 0 else False,
                'mavorgs_invite': _truthy(get(i_inv)) if i_inv >= 0 else False,
                'donation_amount': get(i_don),
            }
            if any(bool(v) for v in row.values()):
                out.append(row)
        log_action('dues_membership_rows', f'total={len(rows)-1}', f'usable={len(out)}')
        _MEMBERSHIP_ROWS_CACHE = list(out)
        _MEMBERSHIP_ROWS_TS = _time.monotonic()
        _set_membership_load_state('sheets', authoritative=True)
        return list(out)
    except Exception as e:
        error_text = str(e)
        log_action('dues_membership_error', 'read', error_text)
        if _MEMBERSHIP_ROWS_CACHE is not None:
            _set_membership_load_state('cache_stale', error=error_text, authoritative=False)
            try:
                log_action('dues_membership_fallback', 'cache_stale', f'usable={len(_MEMBERSHIP_ROWS_CACHE)}')
            except Exception:
                pass
            return list(_MEMBERSHIP_ROWS_CACHE)
        for path in _membership_snapshot_paths():
            try:
                if not path.exists():
                    continue
                out = _load_membership_rows_from_csv(path)
                if not out:
                    continue
                _set_membership_load_state('csv_snapshot', error=error_text, authoritative=False)
                try:
                    log_action('dues_membership_fallback', 'csv_snapshot', f'path={path.name}; usable={len(out)}')
                except Exception:
                    pass
                return out
            except Exception:
                continue
        _set_membership_load_state('error', error=error_text, authoritative=False)
        return []

#--- Semester helpers ---
def _current_semester_label() -> str:
    try:
        from datetime import datetime
        tz = None
        if ZoneInfo is not None:
            try:
                tz = ZoneInfo(getattr(settings, "timezone", "America/Chicago"))
            except Exception:
                tz = None
        now = datetime.now(tz) if tz else datetime.now()
        year = now.year
        sem = 'Spring' if now.month <= 6 else 'Fall'
        return f"{sem} {year}"
    except Exception:
        #safe fallback
        return ""

def _norm_sem_label(s: str) -> str:
    return re.sub(r"\s+", " ", (s or '').strip().title())

def _normalize_paid_where(s: str) -> str:
    t = (s or '').strip().lower()
    if "cash app" in t or "cashapp" in t or "cash-app" in t:
        return "cashapp"
    if "paypal" in t:
        return "paypal"
    if "venmo" in t:
        return "venmo"
    if "zelle" in t or "zelled" in t:
        return "zelle"
    if "cash" in t or "in person" in t or "irl" in t:
        return "cash"
    return ""

def _parse_member_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    #ISO-ish fallback
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        pass
    fmts = [
        "%m/%d/%Y",
        "%m/%d/%y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%y %I:%M:%S %p",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None

def _load_dues_index_ids() -> set[str]:
    """Load processed portal message IDs to avoid reprocessing."""
    global _DUES_INDEX_CACHE, _DUES_INDEX_TS
    ttl = int(getattr(settings, 'dues_index_ttl_sec', 300) or 300)
    now = time.time()
    if _DUES_INDEX_CACHE is not None and (now - _DUES_INDEX_TS) < ttl:
        return set(_DUES_INDEX_CACHE)
    ids: set[str] = set()
    try:
        if os.path.exists(DUES_INDEX):
            with open(DUES_INDEX, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        mid = str(obj.get('message_id') or '')
                        if mid:
                            ids.add(mid)
                    except Exception:
                        continue
    except Exception:
        pass
    _DUES_INDEX_CACHE = set(ids)
    _DUES_INDEX_TS = now
    return ids

def _prep_emails_between(start_dt: datetime, end_dt: datetime) -> list[dict]:
    raw_emails = _load_email_logs_between(start_dt, end_dt)
    prepped: list[dict] = []
    for e in raw_emails:
        subj, body, frm = (e.get('subject','') or ''), (e.get('content','') or ''), (e.get('from','') or '')
        provider = _provider_from_email(frm, subj, body) or ''
        text = subj + ' ' + body
        amount = _extract_amount(text)
        payer_name = _payment_username_from_email({'subject': subj, 'content': body, 'from': frm}) or ''
        ts_utc = None
        try:
            ts_utc = datetime.fromisoformat((e.get('ts_received') or e.get('ts_logged')).replace('Z','+00:00'))
        except Exception:
            ts_utc = None
        prepped.append({
            'id': str(e.get('id') or ''),
            'provider': provider,
            'amount': amount,
            'payer_name': payer_name,
            'ts_utc': ts_utc,
            'raw': e,
        })
    return prepped

def _email_only_candidates(rows: list[dict], cur_sem: str) -> list[tuple[str, str]]:
    """Fallback: verify by membership row + payment email when portal message is missing."""
    cur_sem_norm = _norm_sem_label(cur_sem)
    base_due = float(getattr(settings, 'dues_base_amount', 15.0) or 15.0)
    tol = float(getattr(settings, 'dues_amount_tolerance', 0.01) or 0.01)
    backfill_days = int(getattr(settings, 'dues_email_backfill_days', 30) or 30)
    now = _dues_now()
    def _align_dt(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if now.tzinfo is None:
            return dt.replace(tzinfo=None)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=now.tzinfo)
        return dt.astimezone(now.tzinfo)
    start = now - timedelta(days=backfill_days)
    end = now + timedelta(days=1)
    prepped_emails = _prep_emails_between(start, end)
    if not prepped_emails:
        return []
    out: list[tuple[str, str]] = []
    for r in rows:
        if r.get('verified'):
            continue
        sem = _norm_sem_label(r.get('semester',''))
        if cur_sem_norm and sem and sem != cur_sem_norm:
            continue
        email = (r.get('email') or '').strip().lower()
        if not email:
            continue
        kind = (r.get('kind') or '').lower()
        if 'donat' in kind and 'dues' not in kind and 'verif' not in kind:
            continue
        provider = _normalize_paid_where(r.get('paid_where') or '')
        if provider not in {'venmo','cashapp','paypal'}:
            continue
        r_dt = _align_dt(_parse_member_date(r.get('date') or ''))
        if r_dt and (now - r_dt).days > backfill_days:
            continue
        best_score = 0.0
        best = None
        for E in prepped_emails:
            if E.get('provider') != provider:
                continue
            amt = E.get('amount')
            if amt is None:
                continue
            if amt + tol < base_due:
                continue
            subj = (E.get('raw') or {}).get('subject','')
            body = (E.get('raw') or {}).get('content','')
            text = f"{subj} {body}"
            has_dues_word = bool(_DUES_WORD_RE.search(text))
            if amt > base_due + tol and not has_dues_word:
                continue
            ets = _align_dt(E.get('ts_utc'))
            if ets and (now - ets).days > backfill_days:
                continue
            name_pool = [n for n in [(r.get('full_name') or '').strip(), (r.get('payment_username') or '').strip()] if n]
            if not name_pool or not E.get('payer_name'):
                continue
            overlap = max(_jaccard_tokens(E['payer_name'], n) for n in name_pool)
            if overlap < 0.60:
                continue
            if overlap > best_score:
                best_score = overlap
                best = E
        if best:
            out.append((email, sem or cur_sem))
    #Deduplicate
    seen: set[tuple[str, str]] = set()
    uniq: list[tuple[str, str]] = []
    for e, s in out:
        key = (e, s or '')
        if key in seen:
            continue
        seen.add(key)
        uniq.append((e, s))
    return uniq

def _get_semester_expiry(semester_label: str) -> date:
    """Return the expiration date for a semester's dues role.
    
    Fall expires Jan 31 of the next year.
    Spring expires Sept 15 of the same year.
    """
    from datetime import date
    normalized = (semester_label or '').strip().lower()
    year_match = re.search(r'\d{4}', normalized)
    year = int(year_match.group()) if year_match else datetime.now().year
    
    if 'fall' in normalized:
        #Fall expires Jan 31 of NEXT year
        return date(year + 1, 1, 31)
    elif 'spring' in normalized:
        #Spring expires Sept 15 of SAME year
        return date(year, 9, 15)
    else:
        # Fall back to a date six months ahead when the semester is unknown.
        return (datetime.now() + timedelta(days=180)).date()


def _is_uta_email(s: str) -> bool:
    e = (s or '').strip().lower()
    if not e:
        return False
    return ('@uta.edu' in e) or ('@mavs.uta.edu' in e)

def _parse_loose_date(s: str) -> Optional[datetime]:
    t = (s or '').strip()
    if not t:
        return None
    fmts = ["%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m-%d-%Y", "%m-%d-%y"]
    for f in fmts:
        try:
            return datetime.strptime(t, f)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(t.replace('Z','+00:00')).replace(tzinfo=None)
    except Exception:
        return None

def _dedupe_by_oldest(rows: list[dict]) -> tuple[list[dict], list[str], Dict[str, list[dict]]]:
    """Group by person and keep the oldest row as the canonical info, but aggregate handles.
    Key priority: email -> discord_username (normalized) -> full_name (normalized).
    Returns (deduped_rows, report_lines, group_map).
    """
    groups: Dict[str, list[tuple[dict, Optional[datetime], int]]] = {}
    for idx, r in enumerate(rows):
        email = (r.get('email') or '').strip().lower()
        dk = _norm_user_key(r.get('discord_username') or '')
        nk = _norm_human(r.get('full_name') or '')
        if email:
            key = f"email:{email}"
        elif dk:
            key = f"disc:{dk}"
        elif nk:
            key = f"name:{nk}"
        else:
            key = f"row:{idx}"
        dt = _parse_loose_date(r.get('date') or '')
        groups.setdefault(key, []).append((r, dt, idx))

    deduped: list[dict] = []
    report: list[str] = []
    group_map: Dict[str, list[dict]] = {}
    for key, items in groups.items():
        items_sorted = sorted(items, key=lambda t: (t[1] or datetime.max, t[2]))
        keep_row, keep_dt, _ = items_sorted[0]
        #Aggregate handles across all grouped rows
        agg_handles: list[str] = []
        group_rows: list[dict] = []
        for r0, _, _ in items_sorted:
            group_rows.append(r0)
            if r0.get('discord_username'):
                agg_handles.append(str(r0.get('discord_username')))
            if r0.get('payment_username'):
                agg_handles.append(str(r0.get('payment_username')))
        keep_row['__agg_handles'] = ', '.join([a for a in agg_handles if a])
        deduped.append(keep_row)
        group_map[key] = group_rows
        if len(items_sorted) > 1:
            def fmt(dt: Optional[datetime]) -> str:
                return (dt.strftime('%Y-%m-%d') if dt else 'unknown')
            dates = [fmt(x[1]) for x in items_sorted]
            report.append(f"Duplicate submissions for {key}: kept {fmt(keep_dt)}; also had {', '.join(dates[1:])}")
    return deduped, report, group_map

def _simplify_username(s: str) -> str:
    t = (s or '').strip()
    #Drop leading '@' and discriminator suffix
    t = t.lstrip('@')
    if '#' in t:
        t = t.split('#', 1)[0]
    #Normalize whitespace and lowercase
    t = re.sub(r"\s+", "", t)  #collapse and remove spaces entirely for display-name patterns like "e l u s i v e"
    return t.lower()

def _norm_user_key(s: str) -> str:
    """Strict normalization: keep only [a-z0-9] for robust matching across punctuation/spacing differences."""
    return re.sub(r"[^a-z0-9]+", "", _simplify_username(s))

def _edit_distance(a: str, b: str) -> int:
    a = (a or ""); b = (b or "")
    n, m = len(a), len(b)
    if n == 0: return m
    if m == 0: return n
    dp = list(range(m+1))
    for i in range(1, n+1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, m+1):
            tmp = dp[j]
            cost = 0 if a[i-1] == b[j-1] else 1
            dp[j] = min(dp[j] + 1, dp[j-1] + 1, prev + cost)
            prev = tmp
    return dp[m]


#============================
#MavOrgs invite confirmation + sheet update
#============================

class InvitesConfirmView(discord.ui.View):
    def __init__(self, author_id: int, emails: list[str]):
        super().__init__(timeout=300)  #5 min
        self.author_id = int(author_id) if author_id else 0
        self.emails = [e.strip().lower() for e in emails if e]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            user = interaction.user
            if not user:
                return False
            #Allow original author
            if int(user.id) == self.author_id:
                return True
            #Allow any officer role
            if is_officer(user, settings):
                return True
            await interaction.response.send_message("Only officers can confirm.", ephemeral=True)
        except Exception:
            pass
        return False

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, custom_id="mavorgs_yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass
        ok, msg = await _mark_mavorg_invites(self.emails)
        try:
            await interaction.followup.send(msg or ("Marked invites for " + str(len(self.emails)) + " emails."))
        except Exception:
            pass
        try:
            self.stop()
        except Exception:
            pass

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, custom_id="mavorgs_no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass
        #Delete the original message to clean up the chat
        try:
            await interaction.message.delete()
        except Exception:
            pass
        try:
            self.stop()
        except Exception:
            pass


async def _mark_mavorg_invites(emails: list[str]) -> tuple[bool, str]:
    """Mark 'Mavorgs Invite' TRUE for rows whose email is in the provided list.
    Returns (ok, message)
    """
    emails_set = {e.strip().lower() for e in emails if e}
    if not emails_set:
        return False, "No emails to mark."
    try:
        from ..services.sheets_client import sheets_client as _sc
        sid = getattr(settings, 'sheet_megasheet_id', None)
        if not sid:
            return False, "Missing sheet id for membership megasheet."
        ws_name = getattr(settings,'membership_ws_title','Membership Application List')
        ws = _sc().open_by_key(sid).worksheet(ws_name)
        rows = ws.get_all_values()
        if not rows:
            return False, "Sheet is empty."
        def hkey(s: str) -> str:
            return re.sub(r"[^a-z]+", "", (s or '').lower())
        header_idx = 0
        header = rows[0]
        #Try to locate a better header row within first 30
        target_keys = {'email','mavorgsinvite'}
        best_hits = -1
        for i in range(min(30, len(rows))):
            rk = {hkey(c) for c in rows[i] if c}
            hits = len(rk & target_keys)
            if hits > best_hits and hits >= 1:
                best_hits = hits
                header_idx = i
        header = rows[header_idx]
        idx = {hkey(h): i for i, h in enumerate(header)}
        i_email = idx.get('email', -1)
        i_inv   = idx.get('mavorgsinvite', -1)
        if i_email < 0 or i_inv < 0:
            return False, "Could not find Email or Mavorgs Invite columns."
        #Batch updates: collect cells to update
        updates = []
        for ri, row in enumerate(rows[header_idx+1:], start=header_idx+2):  #1-based rows
            email_val = str(row[i_email] if i_email < len(row) else '').strip().lower()
            if email_val and email_val in emails_set:
                #Only set if different/not already 'TRUE'
                cur = (row[i_inv] if i_inv < len(row) else '').strip().upper()
                if cur not in {'TRUE','✅','YES','Y','1','X'}:
                    updates.append((ri, i_inv+1, 'TRUE'))
        if not updates:
            return True, "All selected rows already marked invited."
        #Prefer a batched update to avoid write quota errors
        try:
            from gspread.cell import Cell  #type: ignore
            cells = [Cell(row=r, col=c, value=v) for r, c, v in updates]
            #Chunk large updates to be gentle with API quotas
            BATCH = 200
            total = 0
            for i in range(0, len(cells), BATCH):
                chunk = cells[i:i+BATCH]
                try:
                    ws.update_cells(chunk, value_input_option='USER_ENTERED')
                    total += len(chunk)
                except Exception as e2:
                    log_action('mavorgs_invite_update_error', f"batch={i}//{BATCH}", str(e2))
                    #Fallback to per-cell with exponential backoff on quota
                    delay = 1.0
                    for cell in chunk:
                        for attempt in range(5):
                            try:
                                ws.update_cell(cell.row, cell.col, cell.value)
                                break
                            except Exception as e3:
                                msg = str(e3).lower()
                                if 'quota' in msg or '429' in msg:
                                    await asyncio.sleep(delay)
                                    delay = min(delay * 2.0, 8.0)
                                    continue
                                log_action('mavorgs_invite_update_error', f"r={cell.row} c={cell.col}", str(e3))
                                break
                #Small pause between chunks
                await asyncio.sleep(0.8)
            return True, f"Marked invites for {len(updates)} member(s)."
        except Exception as e:
            #If we couldn't batch, fall back to per-cell updates with backoff
            log_action('mavorgs_invite_update_error', 'batch_setup', str(e))
            delay = 1.0
            done = 0
            for r, c, v in updates:
                for attempt in range(5):
                    try:
                        ws.update_cell(r, c, v)
                        done += 1
                        break
                    except Exception as e2:
                        msg = str(e2).lower()
                        if 'quota' in msg or '429' in msg:
                            await asyncio.sleep(delay)
                            delay = min(delay * 2.0, 8.0)
                            continue
                        log_action('mavorgs_invite_update_error', f"r={r} c={c}", str(e2))
                        break
            return True, f"Marked invites for {done} member(s)."
    except Exception as e:
        try:
            log_action('mavorgs_invite_update_error', 'sheet', str(e))
        except Exception:
            pass
    return False, f"Error updating sheet: {e}"

def _parse_money_value(text: str) -> Optional[float]:
    if not text:
        return None
    try:
        cleaned = text.replace(',', '')
        match = _MONEY_RE.search(cleaned)
        if not match:
            return None
        return float(match.group(0))
    except Exception:
        return None


def _format_money_value(amount: float) -> str:
    try:
        rounded = round(float(amount), 2)
    except Exception:
        rounded = float(amount)
    return f"${rounded:.2f}"


async def _mark_verified_emails(emails_with_sem: list[tuple[str, str | None]]) -> tuple[bool, str]:
    """Mark 'Verified' TRUE for rows whose email is in provided list and, if given, semester matches.
    Input: list of (email, semester_or_None).
    Returns (ok, message).
    """
    emails_with_sem = [((e or '').strip().lower(), (s or '').strip() if s else None) for e, s in emails_with_sem if e]
    if not emails_with_sem:
        return False, "No emails to mark."
    try:
        from ..services.sheets_client import sheets_client as _sc
        sid = getattr(settings, 'sheet_megasheet_id', None)
        if not sid:
            return False, "Missing sheet id for membership megasheet."
        ws_name = getattr(settings,'membership_ws_title','Membership Application List')
        ws = _sc().open_by_key(sid).worksheet(ws_name)
        rows = ws.get_all_values()
        if not rows:
            return False, "Sheet is empty."
        def hkey(s: str) -> str:
            return re.sub(r"[^a-z]+", "", (s or '').lower())
        header_idx = 0
        #Try to locate a better header row within first 30
        target_keys = {'email','verified','semester'}
        best_hits = -1
        for i in range(min(30, len(rows))):
            rk = {hkey(c) for c in rows[i] if c}
            hits = len(rk & target_keys)
            if hits > best_hits and hits >= 1:
                best_hits = hits
                header_idx = i
        header = rows[header_idx]
        idx = {hkey(h): i for i, h in enumerate(header)}
        i_email = idx.get('email', -1)
        i_ver   = idx.get('verified', -1)
        i_sem   = idx.get('semester', -1)
        if i_email < 0 or i_ver < 0:
            return False, "Could not find Email or Verified columns."
        #Build lookup set
        lookup: dict[str, set[str]] = {}
        for e, s in emails_with_sem:
            if not e:
                continue
            lookup.setdefault(e, set()).add(str(s or '').strip())
        #Prepare Cell updates similar to _mark_mavorg_invites
        from gspread.cell import Cell  #type: ignore
        cells: list[Cell] = []
        marked = 0
        for ri, row in enumerate(rows[header_idx+1:], start=header_idx+2):
            email_val = (row[i_email] if i_email < len(row) else '').strip().lower()
            if not email_val or email_val not in lookup:
                continue
            want = lookup[email_val]
            if i_sem >= 0 and want and '' not in want:
                cur_sem = str(row[i_sem] if i_sem < len(row) else '').strip()
                if cur_sem not in want:
                    continue
            cur_ver = str(row[i_ver] if i_ver < len(row) else '').strip().lower()
            if cur_ver in {'true','yes','y','1','x','✅'}:
                continue
            cells.append(Cell(row=ri, col=i_ver+1, value='TRUE'))
            marked += 1
        if not cells:
            return True, "No rows needed marking as Verified."
        #Chunked updates with backoff like invites
        BATCH = 200
        done = 0
        for i in range(0, len(cells), BATCH):
            chunk = cells[i:i+BATCH]
            try:
                ws.update_cells(chunk, value_input_option='USER_ENTERED')
                done += len(chunk)
            except Exception as e2:
                log_action('mavorgs_verify_update_error', f"batch={i}//{BATCH}", str(e2))
                delay = 1.0
                for cell in chunk:
                    for attempt in range(5):
                        try:
                            ws.update_cell(cell.row, cell.col, cell.value)
                            done += 1
                            break
                        except Exception as e3:
                            msg = str(e3).lower()
                            if 'quota' in msg or '429' in msg:
                                await asyncio.sleep(delay)
                                delay = min(delay * 2.0, 8.0)
                                continue
                            log_action('mavorgs_verify_update_error', f"r={cell.row} c={cell.col}", str(e3))
                            break
            await asyncio.sleep(0.8)
        return True, f"Marked Verified for {done} row(s)."
    except Exception as e:
        try:
            log_action('mavorgs_verify_update_error', 'sheet', str(e))
        except Exception:
            pass
        return False, f"Error updating sheet: {e}"


async def _update_donation_amounts(entries: list[tuple[str, Optional[str], float]]) -> tuple[bool, str]:
    """Set the donation column to the provided amount (email+semester scoped)."""
    cleaned: list[tuple[str, Optional[str], float]] = []
    for email, sem, amount in entries:
        email_norm = (email or '').strip().lower()
        if not email_norm:
            continue
        try:
            amt = float(amount)
        except Exception:
            continue
        if amt <= 0:
            continue
        sem_norm = _norm_sem_label(sem or '') if sem else None
        cleaned.append((email_norm, sem_norm, round(amt, 2)))
    if not cleaned:
        return False, "No donation updates required."

    #Deduplicate by (email, semester)
    desired: Dict[tuple[str, str], float] = {}
    for email_norm, sem_norm, amt in cleaned:
        key = (email_norm, sem_norm or '')
        if key not in desired:
            desired[key] = amt

    try:
        from ..services.sheets_client import sheets_client as _sc
        sid = getattr(settings, 'sheet_megasheet_id', None)
        if not sid:
            return False, "Missing sheet id for membership megasheet."
        ws_name = getattr(settings, 'membership_ws_title', 'Membership Application List')
        ws = _sc().open_by_key(sid).worksheet(ws_name)
        rows = ws.get_all_values()
        if not rows:
            return False, "Sheet is empty."

        def hkey(s: str) -> str:
            return re.sub(r"[^a-z]+", "", (s or '').lower())

        header_idx = 0
        target_keys = {'email', 'donation', 'donations', 'donationamount'}
        best_hits = -1
        for i in range(min(30, len(rows))):
            rk = {hkey(c) for c in rows[i] if c}
            hits = len(rk & target_keys)
            if hits > best_hits and hits >= 1:
                best_hits = hits
                header_idx = i

        header = rows[header_idx]
        idx = {hkey(h): i for i, h in enumerate(header)}
        i_email = idx.get('email', -1)
        i_don = idx.get('donation', -1)
        if i_don < 0:
            i_don = idx.get('donations', -1)
        if i_don < 0:
            i_don = idx.get('donationamount', -1)
        if i_don < 0:
            i_don = idx.get('donation?', -1)
        i_sem = idx.get('semester', -1)
        if i_email < 0 or i_don < 0:
            return False, "Could not find Email or Donation columns."

        from gspread.cell import Cell  #type: ignore

        cells: list[Cell] = []
        applied: set[tuple[str, str]] = set()
        for ri, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
            email_val = str(row[i_email] if i_email < len(row) else '').strip().lower()
            if not email_val:
                continue
            sem_val = _norm_sem_label(row[i_sem]) if (i_sem >= 0 and i_sem < len(row) and row[i_sem]) else None
            candidate_keys = []
            if sem_val:
                candidate_keys.append((email_val, sem_val))
            candidate_keys.append((email_val, ''))

            chosen_key: Optional[tuple[str, str]] = None
            amt_target: Optional[float] = None
            for key in candidate_keys:
                if key in desired and key not in applied:
                    amt_target = desired[key]
                    chosen_key = key
                    break
            if chosen_key is None or amt_target is None:
                continue

            current_val = (row[i_don] if i_don < len(row) else '').strip()
            current_amt = _parse_money_value(current_val)
            if current_amt is not None and abs(current_amt - amt_target) <= 0.01:
                applied.add(chosen_key)
                continue

            value = _format_money_value(amt_target)
            cells.append(Cell(row=ri, col=i_don + 1, value=value))
            applied.add(chosen_key)

        if not cells:
            return True, "No donation cells required changes."

        BATCH = 200
        updated = 0
        for i in range(0, len(cells), BATCH):
            chunk = cells[i:i + BATCH]
            try:
                ws.update_cells(chunk, value_input_option='USER_ENTERED')
                updated += len(chunk)
            except Exception as e:
                log_action('dues_donation_update_error', f"batch={i}//{BATCH}", str(e))
                delay = 1.0
                for cell in chunk:
                    for attempt in range(5):
                        try:
                            ws.update_cell(cell.row, cell.col, cell.value)
                            updated += 1
                            break
                        except Exception as e2:
                            msg = str(e2).lower()
                            if 'quota' in msg or '429' in msg:
                                await asyncio.sleep(delay)
                                delay = min(delay * 2.0, 8.0)
                                continue
                            log_action('dues_donation_update_error', f"r={cell.row} c={cell.col}", str(e2))
                            break
            await asyncio.sleep(0.8)

        return True, f"Updated donations for {updated} row(s)."
    except Exception as e:
        try:
            log_action('dues_donation_update_error', 'sheet', str(e))
        except Exception:
            pass
        return False, f"Error updating donations: {e}"

async def _delete_portal_messages(bot, ids: list[int]) -> int:
    """Delete messages by id from the dues portal channel. Returns count deleted."""
    if not ids:
        return 0
    try:
        ch_id = int(getattr(settings, 'ch_due_portal', 0) or 0)
    except Exception:
        ch_id = 0
    if not ch_id:
        return 0
    ch = getattr(bot, 'get_channel', lambda _id: None)(ch_id)
    if not ch:
        return 0
    deleted = 0
    for mid in ids:
        try:
            msg = await ch.fetch_message(int(mid))
        except Exception:
            continue
        if not msg:
            continue
        try:
            await msg.delete()
            deleted += 1
            continue
        except Exception:
            try:
                await msg.add_reaction(_DUES_PROCESSED_EMOJI)
            except Exception:
                pass
            continue
    return deleted

async def _cleanup_portal_messages_for_emails(bot, rows: list[dict], emails_with_sem: list[tuple[str, str | None]]) -> int:
    """Attempt to delete portal messages for verified emails, even when verified via email-only fallback."""
    if not emails_with_sem:
        return 0
    deleted = 0
    try:
        log_ids = _dues_log_message_ids_for_emails(emails_with_sem)
        if log_ids:
            deleted += await _delete_portal_messages(bot, log_ids)
    except Exception:
        pass
    targets: set[str] = set()
    for email, sem in emails_with_sem:
        email_norm = (email or '').strip().lower()
        if not email_norm:
            continue
        sem_norm = _norm_sem_label(sem or '') if sem else ''
        for r in rows:
            if (r.get('email') or '').strip().lower() != email_norm:
                continue
            if sem_norm:
                r_sem = _norm_sem_label(r.get('semester') or '')
                if r_sem and r_sem != sem_norm:
                    continue
            handle = r.get('discord_username') or ''
            key = _norm_user_key(handle)
            if key:
                targets.add(key)
    if not targets:
        return deleted
    cleanup_limit = int(getattr(settings, 'dues_cleanup_scan_limit', 0) or 0)
    msgs = await _fetch_portal_messages(
        bot,
        include_processed=True,
        limit_override=cleanup_limit if cleanup_limit > 0 else None,
    )
    if not msgs:
        return deleted
    now = _dues_now()
    backfill_days = int(getattr(settings, 'dues_email_backfill_days', 30) or 30)
    ids: list[int] = []
    for m in msgs:
        p = _parse_portal_message(m)
        if not _is_explicit_payment_message(p.get('content','')):
            continue
        ts = p.get('ts')
        if isinstance(ts, datetime):
            if now.tzinfo is None:
                ts_cmp = ts.replace(tzinfo=None)
            elif ts.tzinfo is None:
                ts_cmp = ts.replace(tzinfo=now.tzinfo)
            else:
                ts_cmp = ts.astimezone(now.tzinfo)
            if (now - ts_cmp).days > backfill_days:
                continue
        au = _norm_user_key(p.get('author_name') or '')
        ad = _norm_user_key(p.get('author_display') or '')
        if au in targets or ad in targets:
            mid = int(getattr(m, 'id', 0) or 0)
            if mid:
                ids.append(mid)
    if not ids:
        return deleted
    # Remove duplicates so the same portal message is not deleted twice.
    ids = list(dict.fromkeys(ids))
    deleted += await _delete_portal_messages(bot, ids)
    return deleted

async def _cleanup_portal_messages_for_verified_rows(bot, rows: list[dict], cur_sem: str) -> int:
    """Delete portal messages for members already verified in the current semester."""
    cur_sem_norm = _norm_sem_label(cur_sem)
    emails_with_sem: list[tuple[str, str | None]] = []
    targets: set[str] = set()
    for r in rows:
        if not r.get('verified'):
            continue
        if cur_sem_norm:
            sem = _norm_sem_label(r.get('semester') or '')
            if sem and sem != cur_sem_norm:
                continue
        raw_handle = r.get('discord_username') or ''
        email = (r.get('email') or '').strip().lower()
        if email:
            emails_with_sem.append((email, r.get('semester') or None))
        for cand in _split_handle_candidates(raw_handle):
            key = _norm_user_key(cand)
            if key:
                targets.add(key)
    deleted = 0
    if emails_with_sem:
        try:
            log_ids = _dues_log_message_ids_for_emails(emails_with_sem)
            if log_ids:
                deleted += await _delete_portal_messages(bot, log_ids)
        except Exception:
            pass
    if not targets:
        return deleted
    cleanup_limit = int(getattr(settings, 'dues_cleanup_scan_limit', 0) or 0)
    msgs = await _fetch_portal_messages(
        bot,
        include_processed=True,
        limit_override=cleanup_limit if cleanup_limit > 0 else None,
    )
    if not msgs:
        return deleted
    now = _dues_now()
    backfill_days = int(getattr(settings, 'dues_email_backfill_days', 30) or 30)
    ids: list[int] = []
    for m in msgs:
        p = _parse_portal_message(m)
        if not _is_explicit_payment_message(p.get('content','')):
            continue
        ts = p.get('ts')
        if isinstance(ts, datetime):
            if now.tzinfo is None:
                ts_cmp = ts.replace(tzinfo=None)
            elif ts.tzinfo is None:
                ts_cmp = ts.replace(tzinfo=now.tzinfo)
            else:
                ts_cmp = ts.astimezone(now.tzinfo)
            if (now - ts_cmp).days > backfill_days:
                continue
        au = _norm_user_key(p.get('author_name') or '')
        ad = _norm_user_key(p.get('author_display') or '')
        if au in targets or ad in targets:
            mid = int(getattr(m, 'id', 0) or 0)
            if mid:
                ids.append(mid)
    if not ids:
        return deleted
    ids = list(dict.fromkeys(ids))
    deleted += await _delete_portal_messages(bot, ids)
    return deleted

async def handle_update_dues_members(intent, ctx) -> None:
    """Reconcile dues spreadsheet entries with Discord member info."""
    """Analyze dues, auto-verify high-confidence entries, clean up, then run dues perks.

    Steps:
    - Scan and log new emails first (all-in-one convenience).
    - Run the same analysis as dues_check and post the standard summary lines.
    - For each row with score >= 1.20, with a corroborating provider email, and not flagged cash/donation:
        * Mark Verified=TRUE for that row (by email+semester)
        * Delete the original dues-portal message
    - Then run the 'dues perks' flow (emails list, matched usernames, roles, invites prompt).
    - Finally, post a short list of entries that were skipped for review (low confidence or cash/donation).
    """
    ch = ctx.get('channel')
    bot = ctx.get('bot')
    if not ch or not bot:
        return
    global _MEMBERSHIP_ROWS_CACHE, _MEMBERSHIP_ROWS_TS
    
    # Scan and log new payment emails before syncing sheet state.
    email_status_msg = None
    try:
        email_status_msg = await ch.send('Scanning emails…')
    except Exception:
        pass
    
    logged_count = 0
    try:
        async with _EMAIL_LOG_LOCK:
            svc = await _build_gmail_service(ch)
            #Scan recent emails (last 50 by default for dues purposes)
            q = os.getenv("GMAIL_LAST_QUERY", "in:inbox -from:me")
            n = int(getattr(settings, 'dues_email_scan_count', 50) or 50)
            res = await asyncio.to_thread(
                lambda: svc.users().messages().list(userId="me", q=q, maxResults=n, includeSpamTrash=False).execute()
            )
            msgs = res.get("messages", []) if isinstance(res, dict) else []
            if msgs:
                delay = float(getattr(settings, 'gmail_log_manual_delay_sec', 0.25) or 0.25)
                logged_count = await _log_emails_batch(svc, list(msgs)[::-1], delay_sec=delay)
        
        #Update the status message with the result
        if email_status_msg:
            try:
                await email_status_msg.edit(content=f'Logged {logged_count} new email(s).')
            except Exception:
                pass
        
        # Process finance emails from the same Gmail batch.
        try:
            await finance.process_financial_emails(bot)
        except Exception as e:
            log_action('finance_process_error', 'dues_update', str(e))
            
    except RuntimeError:
        #Gmail auth pending
        if email_status_msg:
            try:
                await email_status_msg.edit(content='Gmail auth pending - skipping email scan.')
            except Exception:
                pass
    except Exception as e:
        log_action('dues_email_scan_error', '', str(e))
        if email_status_msg:
            try:
                await email_status_msg.edit(content=f'Email scan error: {e}')
            except Exception:
                pass
    
    #1) Analyze and post the same summary as handle_check_dues
    placeholder = None
    try:
        placeholder = await ch.send('Analyzing dues…')
    except Exception:
        placeholder = None
    rows = await _analyze_dues(bot)
    cur_sem = _current_semester_label()
    cur_sem_norm = _norm_sem_label(cur_sem)
    def _is_current_verified(mem: dict) -> bool:
        if not mem or not mem.get('verified'):
            return False
        sem = _norm_sem_label(mem.get('semester', ''))
        return bool(sem and cur_sem_norm and sem == cur_sem_norm)
    def _conf_label(score: float) -> str:
        if score >= 1.20: return 'high confidence'
        if score >= 0.90: return 'medium-high'
        if score >= 0.60: return 'review'
        return 'low'
    lines = []
    for rec in rows[-15:]:
        best_mem = rec.get('primary_member') or {}
        if _is_current_verified(best_mem):
            continue
        best_email = rec.get('primary_email')
        auth = rec.get('author') or ''
        disp = rec.get('author_display') or auth
        if rec.get('flag_reason') in {'cash','donation'} and not best_email:
            fn = best_mem.get('full_name') or 'No associated form entry found'
            lines.append(f"- Discord: {auth} ({disp}), Real Name: {fn}, Payment App Username: —, Score = FLAGGED FOR REVIEW")
            continue
        if not best_mem:
            lines.append(f"- Discord: {auth} ({disp}), Real Name: No associated form entry found, Payment App Username: —, Score = 0 (No match found)")
            continue
        name = best_mem.get('full_name') or 'No associated form entry found'
        pay_from_email = (rec.get('payment_username_email') or '—')
        score = float(rec.get('score_total') or 0.0)
        lines.append(f"- Discord: {auth} ({disp}), Real Name: {name}, Payment App Username: {pay_from_email}, Score = {score:.2f} ({_conf_label(score)})")
    header = "Recent dues check results:\n"
    summary_text = header + ("\n".join(lines[:15]) if lines else "")
    if placeholder:
        await placeholder.edit(content=summary_text)
    else:
        await safe_send(ch, summary_text)

    #2) Auto-verify high-confidence entries and delete their portal messages
    eligible: list[dict] = []
    donation_updates: list[tuple[str, Optional[str], float]] = []
    base_due = float(getattr(settings, 'dues_base_amount', 15.0) or 15.0)
    tol = float(getattr(settings, 'dues_amount_tolerance', 0.01) or 0.01)
    for rec in rows:
        sc = float(rec.get('score_total') or 0.0)
        S = rec.get('primary_member') or {}
        E = rec.get('primary_email') or None
        if sc < 1.20:
            continue
        if rec.get('flag_reason') in {'cash','donation'} and not rec.get('primary_email'):
            continue
        if not (S and E):
            continue
        prov = ((_provider_from_email((E.get('from') or ''), (E.get('subject') or ''), (E.get('content') or ''))) or '').lower()
        if prov not in {'venmo','cashapp','paypal'}:
            continue
        #Enforce email time window relative to the discord message timestamp
        try:
            from datetime import datetime
            msg_ts = datetime.fromisoformat(str(rec.get('ts')).replace('Z','+00:00'))
        except Exception:
            msg_ts = None
        try:
            email_ts = datetime.fromisoformat(((E.get('ts_received') or E.get('ts_logged') or '')).replace('Z','+00:00'))
        except Exception:
            email_ts = None
        if msg_ts and email_ts:
            delta = abs((email_ts.replace(tzinfo=None) - msg_ts.replace(tzinfo=None)).total_seconds()) / 86400.0
            wnd = float(getattr(settings, 'dues_email_window_days', 5) or 5)
            if delta > wnd:
                backfill_days = float(getattr(settings, 'dues_email_backfill_days', 30) or 30)
                if email_ts <= msg_ts and (msg_ts.replace(tzinfo=None) - email_ts.replace(tzinfo=None)).total_seconds() / 86400.0 <= backfill_days:
                    pass
                else:
                    continue
        #Require amount == 15 or email mentions dues/due in subject/body
        subj = (E.get('subject') or '')
        body = (E.get('content') or '')
        text = f"{subj} {body}"
        amt = _extract_amount(text)
        has_dues_word = bool(_DUES_WORD_RE.search(text))
        if amt is None:
            continue
        if amt + tol < base_due:
            continue
        if amt > base_due + tol and not has_dues_word:
            continue
        donation_extra = 0.0
        if has_dues_word and amt > base_due + tol:
            donation_extra = round(max(0.0, amt - base_due), 2)
        if donation_extra > 0.0:
            rec['donation_extra'] = donation_extra
            email_for_donation = (S.get('email') or '').strip().lower()
            if email_for_donation:
                donation_updates.append((email_for_donation, (S.get('semester') or '').strip() or None, donation_extra))
        eligible.append(rec)

    #Mark Verified for eligible emails (email + semester)
    emails_with_sem: list[tuple[str, str | None]] = []
    for rec in eligible:
        S = rec['primary_member']
        em = (S.get('email') or '').strip().lower()
        sem = (S.get('semester') or '').strip() or None
        if em:
            emails_with_sem.append((em, sem))
    if emails_with_sem:
        ok, msg = await _mark_verified_emails(emails_with_sem)
        try:
            log_action('dues_auto_verify', f"count={len(emails_with_sem)}", msg)
        except Exception:
            pass
        #Invalidate membership cache so perks sees fresh Verified flags
        _MEMBERSHIP_ROWS_CACHE = None
        _MEMBERSHIP_ROWS_TS = 0.0
    #Fallback: email-only verification when portal messages are missing
    rows_for_fallback: list[dict] = []
    extra: list[tuple[str, str]] = []
    try:
        rows_for_fallback = _load_membership_rows()
        extra = _email_only_candidates(rows_for_fallback, cur_sem)
        if extra:
            ok2, msg2 = await _mark_verified_emails(extra)
            try:
                log_action('dues_auto_verify', f"fallback={len(extra)}", msg2)
            except Exception:
                pass
            if ok2:
                _MEMBERSHIP_ROWS_CACHE = None
                _MEMBERSHIP_ROWS_TS = 0.0
    except Exception:
        pass
    if extra:
        name_map: dict[str, str] = {}
        for r in rows_for_fallback:
            em = (r.get('email') or '').strip().lower()
            if not em:
                continue
            if em not in name_map or not name_map[em]:
                name_map[em] = (r.get('full_name') or '').strip()
        fallback_lines: list[str] = []
        for em, sem in extra:
            name = name_map.get(em) or '(unknown)'
            sem_label = sem or cur_sem
            fallback_lines.append(f"- {name} ({em}, {sem_label})")
        fallback_text = "Email-only verified:\n" + "\n".join(fallback_lines)
        if placeholder:
            try:
                await placeholder.edit(content=summary_text + "\n" + fallback_text)
            except Exception:
                await safe_send(ch, fallback_text)
        else:
            await safe_send(ch, fallback_text)
    if extra:
        try:
            deleted_fallback = await _cleanup_portal_messages_for_emails(bot, rows_for_fallback, extra)
            if deleted_fallback:
                log_action('dues_auto_cleanup', f'fallback_deleted={deleted_fallback}', '')
        except Exception:
            pass

    #Delete portal messages for eligible
    ids = [int(rec.get('message_id') or 0) for rec in eligible if int(rec.get('message_id') or 0)]
    if ids:
        deleted = await _delete_portal_messages(bot, ids)
        try:
            log_action('dues_auto_cleanup', f'deleted={deleted}', '')
        except Exception:
            pass

    #Update donation amounts for high-confidence dues + donation emails
    if donation_updates:
        ok_don, msg_don = await _update_donation_amounts(donation_updates)
        try:
            log_action('dues_auto_donation', f"count={len(donation_updates)}", msg_don)
        except Exception:
            pass
        if ok_don:
            _MEMBERSHIP_ROWS_CACHE = None
            _MEMBERSHIP_ROWS_TS = 0.0

    #2b) Cleanup portal messages for already-verified members
    try:
        verified_cleanup = await _cleanup_portal_messages_for_verified_rows(bot, _load_membership_rows(), cur_sem)
        if verified_cleanup:
            log_action('dues_auto_cleanup', f'verified_deleted={verified_cleanup}', '')
    except Exception:
        pass

    #3) Run the standard dues perks flow (emails list, usernames, roles, invite prompt)
    try:
        await handle_run_dues_perks(intent, ctx)
    except Exception as e:
        try:
            log_action('dues_update_perks_error', '', str(e))
        except Exception:
            pass

    #4) Report entries left for manual review
    review_lines: list[str] = []
    for rec in rows:
        sc = float(rec.get('score_total') or 0.0)
        if rec in eligible:
            continue
        best_mem = rec.get('primary_member') or {}
        if _is_current_verified(best_mem):
            continue
        reason = None
        if rec.get('flag_reason') in {'cash','donation'}:
            reason = rec.get('flag_reason')
        elif sc < 1.20:
            reason = f"score={sc:.2f}"
        if reason:
            auth = rec.get('author') or ''
            name = (rec.get('primary_member') or {}).get('full_name') or rec.get('name_guess') or '(unknown)'
            review_lines.append(f"- {auth} → {name} ({reason})")
    if review_lines:
        await safe_send(ch, "Manual review needed for:\n" + "\n".join(review_lines[:30]))

async def _ensure_guild_members(guild, force_fetch: bool = False) -> list:
    members: list = []
    try:
        if not force_fetch:
            members = list(getattr(guild, 'members', []) or [])
            if members:
                return members
        #Fetch full member list when syncing roles (avoid partial cache)
        out = []
        async for m in guild.fetch_members(limit=None):
            out.append(m)
        members = out or members
    except Exception as e:
        try:
            log_action('dues_role_sync_members', 'fetch_error', str(e))
        except Exception:
            pass
    if not members:
        try:
            members = list(getattr(guild, 'members', []) or [])
        except Exception:
            members = []
    return members

def _split_handle_candidates(text: str) -> list[str]:
    """Split a discord 'username' field from the sheet into plausible handle candidates.
    Handles separators like '/', ',', '|', ' or ', ' aka ', ' now ', etc., and filters junk tokens.
    Returns a list of distinct raw candidates (not yet normalized), preserving order.
    """
    t = (text or '').strip()
    if not t:
        return []
    #Quick bail on common non-answers
    if re.fullmatch(r"(?i)\s*(none|n/?a|na|same|no it is the same|not different)\s*", t):
        return []
    #Split on common separators and phrases
    parts = re.split(r"\s*(?:\bor\b|\band\b|/|\\|,|\||\baka\b|\snow\b|\salso\b|\-\s*now\s*)\s*", t, flags=re.I)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        p = p.strip().lstrip('@')
        if not p or re.fullmatch(r"(?i)(none|n/?a|na)", p):
            continue
        #If they provided multiple words, keep if it looks like a username (has dot/underscore/number) or short single word
        keep = False
        if len(p) <= 24 and re.search(r"[._0-9]", p):
            keep = True
        if not keep and ' ' not in p and 2 <= len(p) <= 24:
            keep = True
        if not keep:
            continue
        key = _norm_user_key(p)
        if not key or len(key) < 2:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    #If nothing parsed, return original trimmed (still useful for fuzzy)
    return out or [t]

def _expand_handle_variants(handles: list[str]) -> list[str]:
    """Generate neighbor variants for common typo patterns without exploding search space.
    - If a handle ends with digits, add +/-1 variants.
    - Normalize common spelling: sourcerer <-> sorcerer.
    - Strip parenthetical notes sometimes appended in the membership sheet.
    """
    out: list[str] = []
    seen: set[str] = set()
    for h in handles:
        if not h:
            continue
        def add(s: str):
            k = s.lower().strip()
            if k and k not in seen:
                seen.add(k); out.append(s)
        add(h)
        no_paren = re.sub(r"\s*\([^)]*\)\s*", "", h).strip()
        if no_paren and no_paren != h:
            add(no_paren)
        m = re.search(r"^(.*?)(\d+)$", h)
        if m:
            base, num = m.group(1), m.group(2)
            try:
                n = int(num)
                for d in (1,2,3):
                    add(f"{base}{n+d}")
                    if n - d >= 0:
                        add(f"{base}{n-d}")
            except Exception:
                pass
        #spelling variants
        if 'sourcerer' in h.lower():
            add(re.sub(r"(?i)sourcerer", "sorcerer", h))
        if 'sorcerer' in h.lower():
            add(re.sub(r"(?i)sorcerer", "sourcerer", h))
        #common terminal swap: 'din' <-> 'dan'
        if re.search(r"(?i)din$", h):
            add(re.sub(r"(?i)din$", "dan", h))
        if re.search(r"(?i)dan$", h):
            add(re.sub(r"(?i)dan$", "din", h))
    return out

def _best_member_match(sheet_name: str, full_name: str, members: list) -> tuple[object | None, int, str, str]:
    """Find the best guild member for a given sheet username/full-name.
    Returns (member, score, matched_form, mode).
    mode in {handle_exact, handle_contain, handle_edit, handle_fuzzy, name_fuzzy, none}.
    Score is 0..100.
    """
    #Prepare candidate handles from the sheet (can contain multiple options)
    handle_candidates = _split_handle_candidates(sheet_name) or [sheet_name]
    handle_candidates = _expand_handle_variants(handle_candidates)
    q_full_s_base = _simplify_username(full_name)
    q_full_base = _norm_user_key(full_name)

    #If no reasonable query at all, bail
    if not any(_norm_user_key(h) for h in handle_candidates) and not q_full_base:
        return (None, 0, "", "none")

    def _score_vs_member(m, q_handle_s: str, q_handle: str, q_full_s: str, q_full: str) -> tuple[int, str, str]:
        cands_raw = []
        try:
            cands_raw.append(_simplify_username(getattr(m, 'name', '') or ''))
            cands_raw.append(_simplify_username(getattr(m, 'display_name', '') or ''))
            cands_raw.append(_simplify_username(getattr(m, 'global_name', '') or ''))
        except Exception:
            pass
        cands_raw = [c for c in cands_raw if c]
        cands_key = [_norm_user_key(c) for c in cands_raw]

        #1) Exact alnum equality on handle
        if q_handle and q_handle in cands_key:
            return (100, cands_raw[cands_key.index(q_handle)], "handle_exact")
        #1b) Exact alnum equality on full name-derived key (handles spaced/stylized display names)
        if q_full and q_full in cands_key and len(q_full) >= 4:
            return (96, cands_raw[cands_key.index(q_full)], "handle_contain")

        #2) Containment for keys >= 4 chars (avoid short accidental hits)
        for i, ck in enumerate(cands_key):
            if q_handle and len(q_handle) >= 4 and (q_handle in ck or ck in q_handle):
                return (93, cands_raw[i], "handle_contain")
        for i, ck in enumerate(cands_key):
            if q_full and len(q_full) >= 5 and (q_full in ck or ck in q_full):
                return (90, cands_raw[i], "handle_contain")

        #2b) One- to two-edits-away on handle for longer keys (e.g., kitadin→kitadan, plink5276→plink5277)
        if q_handle_s:
            for i, cr in enumerate(cands_raw):
                ed = _edit_distance(q_handle_s, cr)
                if ed <= 2 and max(len(q_handle_s), len(cr)) >= 6:
                    #Slightly reduce score for 2 edits
                    return (92 if ed == 1 else 88, cr, "handle_edit")

        #3) Fuzzy on simplified strings (prefer handle vs member.name/display/global)
        best_local = 0
        best_form = ""
        best_mode = "none"
        for i, cr in enumerate(cands_raw):
            #Compare sheet handle to candidate raw
            scores = []
            mode_now = "none"
            try:
                if q_handle_s:
                    scores.append(_ratio(q_handle_s, cr))
                    mode_now = "handle_fuzzy"
                if q_full_s:
                    scores.append(_ratio(q_full_s, cr))
                    if not q_handle_s:
                        mode_now = "name_fuzzy"
            except Exception:
                pass
            sc = max(scores) if scores else 0
            if sc > best_local:
                best_local = sc
                best_form = cr
                best_mode = mode_now if scores else "none"
        return (best_local, best_form, best_mode)

    best_member = None
    best_score = 0
    best_form = ""
    best_mode = "none"
    #Try each candidate handle; keep the best scoring member across all
    for cand in handle_candidates:
        q_handle_s = _simplify_username(cand)
        q_handle = _norm_user_key(cand)
        q_full_s = q_full_s_base
        q_full = q_full_base
        for m in members:
            sc, form, mode = _score_vs_member(m, q_handle_s, q_handle, q_full_s, q_full)
            if sc > best_score:
                best_score = sc
                best_member = m
                best_form = form
                best_mode = mode
                if best_score >= 100:
                    break
        if best_score >= 100:
            break
    return (best_member, best_score, best_form, best_mode)

#--- Shared matching helpers for membership ↔ guild ---
def _norm_human(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or '').lower())

def _compute_duplicates(rows: list[dict]) -> dict:
    by_email: dict[str,int] = {}
    by_disc: dict[str,int] = {}
    by_name: dict[str,int] = {}
    for r in rows:
        e = (r.get('email') or '').strip().lower()
        if e:
            by_email[e] = by_email.get(e,0)+1
        d = _norm_user_key(r.get('discord_username') or '')
        if d:
            by_disc[d] = by_disc.get(d,0)+1
        n = _norm_human(r.get('full_name') or '')
        if n:
            by_name[n] = by_name.get(n,0)+1
    dups = {
        'email': {k for k,v in by_email.items() if v>1},
        'discord': {k for k,v in by_disc.items() if v>1},
        'name': {k for k,v in by_name.items() if v>1},
    }
    return dups

async def _guild_and_members(bot):
    guild_id = getattr(settings, "target_guild_id", None) or getattr(settings, "ui_guild_id", None) or 0
    guild = None
    try:
        guild = bot.get_guild(int(guild_id))
    except Exception:
        guild = None
    if not guild:
        return None, []
    members = await _ensure_guild_members(guild)
    return guild, members

def _match_membership_rows_to_members(rows: list[dict], members: list) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (matched_records, possible_records, unmatched_rows).
    matched_records: [{'row': row, 'member': member, 'score': score}]
    possible_records: same shape for mid-confidence.
    unmatched_rows: [row]
    """
    matched: list[dict] = []
    possible: list[dict] = []
    unmatched: list[dict] = []
    #id -> member map
    id_to_member: Dict[int, Any] = {}
    for m in members:
        try:
            mid = int(getattr(m, 'id', 0) or 0)
            if mid:
                id_to_member[mid] = m
        except Exception:
            pass
    #Optional alias map from settings.user_id_map (normalized keys -> member)
    alias_to_member: Dict[str, Any] = {}
    try:
        uid_map = getattr(settings, 'user_id_map', {}) or {}
        for k, uid in uid_map.items():
            try:
                key = _norm_user_key(str(k))
                mid = int(uid) if str(uid).isdigit() else None
                if key and mid and mid in id_to_member:
                    alias_to_member[key] = id_to_member[mid]
            except Exception:
                continue
    except Exception:
        alias_to_member = {}

    for r in rows:
        #Combine all known handles for this person: aggregated from dedupe, or fallback to discord + payment
        sheet_handles = (r.get('__agg_handles') or '').strip()
        if not sheet_handles:
            sheet_handles = ", ".join([
                (r.get('discord_username') or '').strip(),
                (r.get('payment_username') or '').strip(),
            ])
        m, sc, _, mode = _best_member_match(sheet_handles, r.get('full_name') or '', members)
        # Reuse a previously resolved username-to-member binding when available.
        key = _norm_user_key(r.get('discord_username') or '')
        if key and key in _RESOLVED_SHEET_USERNAME_TO_UID:
            mid = _RESOLVED_SHEET_USERNAME_TO_UID[key]
            mm = id_to_member.get(mid)
            if mm is not None:
                matched.append({'row': r, 'member': mm, 'score': 100, 'mode': 'handle_exact'})
                continue
        #Alias fallback (settings.user_id_map)
        if key and key in alias_to_member:
            matched.append({'row': r, 'member': alias_to_member[key], 'score': 100, 'mode': 'handle_exact'})
            continue
        #Alias near-match fallback: allow 1 edit or high token ratio (to catch typos like kitadin -> Kitadan)
        if key and not m and alias_to_member:
            best_alias = None
            best_alias_score = -1
            for ak, mm in alias_to_member.items():
                try:
                    ed = _edit_distance(key, ak)
                    rr = _ratio(key, ak)
                except Exception:
                    ed, rr = 99, 0
                score = 100 - min(ed, 3) * 10 + rr // 10
                if ed <= 1 or rr >= 95:
                    if score > best_alias_score:
                        best_alias_score = score
                        best_alias = mm
            if best_alias is not None:
                matched.append({'row': r, 'member': best_alias, 'score': 95, 'mode': 'alias_near'})
                continue
        #Accept only strong handle-based matches; promote strong name-only fuzzy too
        if m and mode.startswith('handle') and (sc >= 85 or (key and sc >= 82)):
            matched.append({'row': r, 'member': m, 'score': sc, 'mode': mode})
        elif m and mode == 'name_fuzzy' and sc >= 90:
            matched.append({'row': r, 'member': m, 'score': sc, 'mode': mode})
        elif m and 70 <= sc < 95:
            possible.append({'row': r, 'member': m, 'score': sc, 'mode': mode})
        else:
            unmatched.append(r)
    #Enforce one-person-per-username by keeping highest score per member id
    best_for_uid: Dict[int, dict] = {}
    def _mode_rank(m: str) -> int:
        return {"handle_exact": 3, "handle_edit": 2, "handle_contain": 1, "handle_fuzzy": 0, "name_fuzzy": -1}.get(m or "", -2)
    for rec in matched:
        uid = int(getattr(rec['member'], 'id', 0) or 0)
        if not uid:
            continue
        prev = best_for_uid.get(uid)
        if not prev:
            best_for_uid[uid] = rec
            continue
        if rec['score'] > prev['score'] or (rec['score'] == prev['score'] and _mode_rank(rec.get('mode')) > _mode_rank(prev.get('mode'))):
            best_for_uid[uid] = rec
    matched_unique = list(best_for_uid.values())

    #Any matched records dropped by member de-dup become unmatched candidates for re-try/reporting
    kept_rows = {id(rec['row']) for rec in matched_unique}
    dropped_rows = [rec['row'] for rec in matched if id(rec['row']) not in kept_rows]
    if dropped_rows:
        row_ids = {id(r) for r in unmatched}
        for r in dropped_rows:
            if id(r) not in row_ids:
                unmatched.append(r)

    #Second pass: promote high-score possibles to fill unused members/rows
    used_uids = {int(getattr(rec['member'], 'id', 0) or 0) for rec in matched_unique if rec.get('member')}
    matched_rows = {id(rec['row']) for rec in matched_unique}
    #Sort possibles by score desc, prefer handle modes
    def _pos_rank(rec: dict) -> tuple:
        mode = rec.get('mode') or ''
        mode_boost = 2 if str(mode).startswith('handle') else (1 if mode == 'name_fuzzy' else 0)
        return (-int(rec.get('score') or 0), -mode_boost)
    for rec in sorted(possible, key=_pos_rank):
        uid = int(getattr(rec['member'], 'id', 0) or 0)
        if not uid or uid in used_uids:
            continue
        if id(rec['row']) in matched_rows:
            continue
        #Promote if reasonably strong
        sc = int(rec.get('score') or 0)
        mode = rec.get('mode') or ''
        if (mode.startswith('handle') and sc >= 72) or (mode == 'name_fuzzy' and sc >= 85) or sc >= 88:
            matched_unique.append(rec)
            used_uids.add(uid)
            matched_rows.add(id(rec['row']))

    #Third pass: reassign unmatched/dropped rows to unused members (greedy), trying second-best options
    remaining_members = [m for m in members if int(getattr(m, 'id', 0) or 0) not in used_uids]
    if remaining_members:
        still_unmatched: list[dict] = []
        for r in unmatched:
            sheet_handles = (r.get('__agg_handles') or '').strip()
            if not sheet_handles:
                sheet_handles = ", ".join([
                    (r.get('discord_username') or '').strip(),
                    (r.get('payment_username') or '').strip(),
                ])
            m2, sc2, _, mode2 = _best_member_match(sheet_handles, r.get('full_name') or '', remaining_members)
            if m2:
                uid2 = int(getattr(m2, 'id', 0) or 0)
                if uid2 and uid2 not in used_uids:
                    if (str(mode2).startswith('handle') and sc2 >= 68) or (mode2 == 'name_fuzzy' and sc2 >= 82) or sc2 >= 86:
                        matched_unique.append({'row': r, 'member': m2, 'score': sc2, 'mode': mode2})
                        used_uids.add(uid2)
                        continue
            still_unmatched.append(r)
        unmatched = still_unmatched

    return matched_unique, possible, unmatched

def _find_sandbox_channel(bot):
    try:
        ch_id = int(getattr(settings, 'ch_sandbox', None) or 0)
        if not ch_id:
            return None
        return bot.get_channel(ch_id)
    except Exception:
        return None

async def handle_run_dues_perks(intent, ctx) -> None:
    """Grant membership perks to users whose dues are verified."""
    """Reply with UTA emails of verified members for the current semester, then a second reply with matched Discord usernames.

    - Semester: Spring (Jan-Jun) or Fall (Jul-Dec), current year.
    - Verified: uses the 'Verified?' (or similar) column; truthy values accepted.
    - Emails: only '@uta.edu' or '@mavs.uta.edu'.
    - Usernames: fuzzy match sheet 'Discord Username' (or Full Name) against actual guild members.
    """
    ch = ctx.get('channel')
    bot = ctx.get('bot')
    author = ctx.get('author')
    is_admin = is_officer(author, settings)
    if not is_admin:
        log_action("dues_perks_denied", f"user={getattr(author,'id',0)}", "not_officer")
        return
    if not ch or not bot:
        return

    #Load membership rows
    rows = _load_membership_rows()
    cur_sem = _current_semester_label()
    cur_sem_norm = _norm_sem_label(cur_sem)

    #Filter by semester + verified
    use_rows = []
    for r in rows:
        sem = _norm_sem_label(r.get('semester',''))
        if sem and cur_sem_norm and sem != cur_sem_norm:
            continue
        if not r.get('verified', False):
            #If verified column missing, fallback to keywords in kind/paid_where
            pass
        if not r.get('verified', False):
            continue
        use_rows.append(r)

    #Dedupe by person (keep oldest info) and aggregate handles across dupes
    use_rows_dedup, dup_reports, _groups = _dedupe_by_oldest(use_rows)

    #1) Emails message (to current channel)
    #Only include valid UTA emails where MavOrgs Invite has NOT been sent (any of their grouped rows)
    def _group_key(r: dict) -> Optional[str]:
        email = (r.get('email') or '').strip().lower()
        dk = _norm_user_key(r.get('discord_username') or '')
        nk = _norm_human(r.get('full_name') or '')
        if email:
            return f"email:{email}"
        if dk:
            return f"disc:{dk}"
        if nk:
            return f"name:{nk}"
        return None
    emails: list[str] = []
    for r in use_rows_dedup:
        gk = _group_key(r)
        grp = _groups.get(gk, [r]) if gk else [r]
        invite_sent = any(bool(rr.get('mavorgs_invite')) for rr in grp)
        if invite_sent:
            continue
        raw = r.get('email') or ''
        parts = [p.strip() for p in re.split(r"[\s,;]+", raw) if p.strip()]
        for e in parts:
            if _is_uta_email(e):
                emails.append(e.lower())
    seen = set(); emails_unique: list[str] = []
    for e in emails:
        if e not in seen:
            seen.add(e); emails_unique.append(e)
    if emails_unique:
        await safe_send(ch, "MavOrgs student email list:")
        await safe_send(ch, "\n".join(emails_unique))
    else:
        await safe_send(ch, "No pending MavOrgs emails for verified members this semester.")

    #2) Username matching and reporting
    guild, members = await _guild_and_members(bot)
    if not guild or not members:
        await safe_send(ch, "Could not access the target guild to match usernames.")
        return

    matched, possible, unmatched = _match_membership_rows_to_members(use_rows_dedup, members)
    #Remember resolved sheet usernames -> member ids for future runs
    for rec in matched:
        r = rec['row']; mem = rec['member']
        key = _norm_user_key(r.get('discord_username') or '')
        try:
            mid = int(getattr(mem, 'id', 0) or 0)
        except Exception:
            mid = 0
        if key and mid:
            _RESOLVED_SHEET_USERNAME_TO_UID[key] = mid
    #Prepare summary for current channel (with coverage)
    matched_labels = []
    seen_labels: set[str] = set()
    for m in matched:
        mem = m.get('member')
        if not mem:
            continue
        label = getattr(mem, 'name', None) or getattr(mem, 'display_name', None) or getattr(mem, 'global_name', None)
        if not label:
            continue
        key = label.strip().lower()
        if key in seen_labels:
            continue
        seen_labels.add(key)
        matched_labels.append(label)
    matched_labels.sort()
    #For unmatched, show full row context (name, discord, email)
    def _handles_for_row(r: dict) -> str:
        vals = []
        if r.get('discord_username'): vals.append(str(r.get('discord_username')).strip())
        if r.get('payment_username'): vals.append(str(r.get('payment_username')).strip())
        if r.get('__agg_handles'): vals.append(str(r.get('__agg_handles')).strip())
        h = ' | '.join([v for v in vals if v])
        return h
    unmatched_entries = []
    for r in unmatched:
        ver_val = str(r.get('verified') or '').strip().lower()
        if ver_val in {'true','yes','y','1','x','✅'}:
            continue
        unmatched_entries.append(
            f"{(r.get('full_name') or '').strip() or '(unknown name)'} — Discord: {(r.get('discord_username') or '').strip() or '(unknown)'} — Pay: {(r.get('payment_username') or '').strip() or '(unknown)'} — Tried: {_handles_for_row(r)} — Email: {(r.get('email') or '').strip()}"
        )
    possible_rows = sorted([(r['row'].get('discord_username') or r['row'].get('full_name') or '(unknown)'), (getattr(r['member'],'name','') or getattr(r['member'],'display_name','') or ''), int(r['score'])] for r in possible)
    #Prepare labels for sandbox error summary
    unmatched_labels = sorted({
        ((r.get('discord_username') or '').strip() or (r.get('full_name') or '').strip() or '(unknown)')
        for r in unmatched
        if (r.get('discord_username') or r.get('full_name'))
        and str(r.get('verified') or '').strip().lower() not in {'true','yes','y','1','x','✅'}
    })
    parts: list[str] = []
    if unmatched_entries:
        parts.append("Unmatched verified entries:\n" + "\n".join(unmatched_entries))
    if possible_rows:
        poss = ", ".join(f"{src} → {cand} ({sc}%)" for src, cand, sc in possible_rows[:20])
        parts.append("Possible matches: " + poss)
    if parts:
        await safe_send(ch, "\n".join(parts))

    #3) Errors to sandbox (unmatched + possible + duplicates)
    sandbox = _find_sandbox_channel(bot)
    dups = _compute_duplicates(use_rows)
    if sandbox and hasattr(sandbox, 'send'):
        err_lines: list[str] = []
        if dup_reports:
            err_lines.append("Duplicates (kept oldest):")
            err_lines.extend(dup_reports[:30])
        if unmatched_labels:
            err_lines.append("Unmatched usernames: " + ", ".join(unmatched_labels))
        if possible_rows:
            poss = ", ".join(f"{src} → {cand} ({sc}%)" for src, cand, sc in possible_rows[:30])
            err_lines.append("Possible matches: " + poss)
        dup_bits: list[str] = []
        if dups['email']:
            dup_bits.append("email=" + ", ".join(sorted(dups['email'])[:20]))
        if dups['discord']:
            dup_bits.append("discord=" + ", ".join(sorted(dups['discord'])[:20]))
        if dups['name']:
            dup_bits.append("name=" + ", ".join(sorted(list(dups['name']))[:20]))
        if dup_bits:
            err_lines.append("Duplicates: " + "; ".join(dup_bits))
        if err_lines:
            await safe_send(sandbox, "Dues Perks Issues\n" + "\n".join(err_lines))

    #4) Status messages for confident matches (posted to sandbox), 1.1s apart
    confident = []
    dup_keys = dups
    for rec in matched:
        r = rec['row']; mem = rec['member']
        em = (r.get('email') or '').strip().lower()
        dk = _norm_user_key(r.get('discord_username') or '')
        nk = _norm_human(r.get('full_name') or '')
        is_dup = (em and em in dup_keys['email']) or (dk and dk in dup_keys['discord']) or (nk and nk in dup_keys['name'])
        if is_dup:
            continue
        confident.append((r, mem))

    #Choose destination for status messages: member-names channel preferred
    dest = None
    try:
        member_ch_id = int(getattr(settings, 'ch_member_names', None) or 0)
    except Exception:
        member_ch_id = 0
    if member_ch_id and bot:
        try:
            dest = bot.get_channel(member_ch_id)
        except Exception:
            dest = None
    if not dest:
        dest = sandbox or ch

    #Role to grant to verified/matched members
    ROLE_DUES_ID = int(getattr(settings, "role_dues_perks_id", 0) or 0)
    role_obj = None
    try:
        role_obj = guild.get_role(int(ROLE_DUES_ID)) if guild else None
    except Exception:
        role_obj = None
    sem_label = (_norm_sem_label(cur_sem) or cur_sem).lower()

    #Load existing posts in destination to avoid duplicate semester entries per member
    existing_lines: list[str] = []
    try:
        if hasattr(dest, 'history'):
            async for msg in dest.history(limit=1000):
                try:
                    #Prefer filtering to bot-authored messages if bot_user_id configured
                    bot_uid = int(getattr(settings, 'bot_user_id', 0) or 0)
                    if bot_uid and int(getattr(msg.author, 'id', 0) or 0) != bot_uid:
                        continue
                except Exception:
                    pass
                content = (getattr(msg, 'content', '') or '').strip()
                if content:
                    existing_lines.append(content.lower())
    except Exception:
        pass

    for r, mem in confident:
        #Add the dues role to matched member (without altering other roles)
        if role_obj is not None:
            try:
                roles = getattr(mem, 'roles', []) or []
                if role_obj not in roles and hasattr(mem, 'add_roles'):
                    await mem.add_roles(role_obj, reason="TomCat: grant dues role for verified semester")
            except Exception as e:
                try:
                    log_action('dues_perks_role_add_error', f"uid={getattr(mem,'id',0)}", str(e))
                except Exception:
                    pass
        real = r.get('full_name') or '(unknown)'
        uname = getattr(mem, 'name', '') or getattr(mem, 'display_name', '') or '(unknown)'
        line = f"{real}, {uname}, {sem_label}"
        #If we've already posted an entry for this username + semester in the destination, skip posting
        uname_l = (uname or '').strip().lower()
        already = False
        for t in existing_lines:
            if uname_l and (uname_l in t) and (sem_label in t):
                already = True
                break
        if not already:
            await safe_send(dest, line)
            #Keep a lower-case copy so subsequent entries in this run dedupe too
            existing_lines.append(line.lower())
            await asyncio.sleep(1.1)

    #Ask to confirm MavOrgs invites and mark sheet when confirmed
    try:
        #Only prompt if we actually produced a non-empty email list
        if emails_unique:
            orig_msg = ctx.get('message')
            requester = ctx.get('author')
            if orig_msg and requester:
                view = InvitesConfirmView(int(getattr(requester,'id',0) or 0), emails_unique)
                await safe_send(ch, "Have the MavOrgs invites been sent?", reference=orig_msg, view=view)
    except Exception as e:
        try:
            log_action('mavorgs_invite_prompt_error', '', str(e))
        except Exception:
            pass

#--- Email logs (existing helpers retained) ---

_MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec",
}

def _to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    try:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None)

def _email_month_paths_between(start_dt: datetime, end_dt: datetime) -> List[str]:
    files = []
    seen = set()
    cur = datetime(start_dt.year, start_dt.month, 1)
    while cur <= end_dt:
        mon_name = _MONTH_NAMES.get(cur.month, f"{cur.month:02d}")
        p = os.path.join(EMAILS_DIR, f"{cur.year}-{mon_name}.ndjson")
        if p not in seen:
            files.append(p); seen.add(p)
        if cur.month == 12:
            cur = datetime(cur.year+1, 1, 1)
        else:
            cur = datetime(cur.year, cur.month+1, 1)
    return files

def _load_email_logs_between(start_dt: datetime, end_dt: datetime) -> List[dict]:
    start_dt = _to_utc_naive(start_dt)
    end_dt = _to_utc_naive(end_dt)
    paths = _email_month_paths_between(start_dt, end_dt)
    out: List[dict] = []
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        obj = json.loads(line)
                        if obj.get('event') != 'email_received':
                            continue
                        ts = obj.get('ts_received') or obj.get('ts_logged')
                        if not ts:
                            continue
                        ts2 = datetime.fromisoformat(ts.replace('Z','+00:00')).replace(tzinfo=None)
                        if start_dt <= ts2 <= end_dt:
                            out.append(obj)
                    except Exception:
                        continue
        except Exception as e:
            log_action('dues_email_read_error', p, str(e))
    return out

#--- Provider & payer extraction from emails ---

def _provider_from_email(frm: str, subj: str, body: str) -> Optional[str]:
    f = (frm or '').lower(); s = (subj or '').lower(); b = (body or '').lower()
    if 'venmo.com' in f:
        if ('paid you' in s) or ('sent you' in s) or ('paid you' in b):
            return 'venmo'
    if 'cash.app' in f or 'squareup.com' in f or 'square.com' in f:
        # Cash App notices often use "Payment received" in the subject and a
        # sender-name pattern in the body.
        if ('paid you' in s) or ('sent you' in s) or ('paid you' in b) or ('payment received' in s) or ('you were sent' in b):
            return 'cashapp'
    if 'paypal.com' in f:
        if ("you've got money" in s) or ("sent you" in s) or ('payment received' in s) or ('paid you' in b):
            return 'paypal'
    return None

def _extract_amount(text: str) -> Optional[float]:
    candidates = _amount_candidates(text)
    if not candidates:
        return None
    best = None
    for val in candidates:
        if best is None or val > best:
            best = val
    return best

def _payment_username_from_email(em: dict) -> str | None:
    subj = em.get('subject') or ''
    body = em.get('content') or ''
    frm = em.get('from') or ''
    prov = detect_provider(frm, subj, body)
    if prov == 'venmo':
        m = re.search(r"^\s*([^\n]+?)\s+paid you\b", subj, re.I)
        if m:
            return m.group(1).strip()
        m2 = re.search(r"([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,2})", subj)
        if m2:
            return m2.group(1)
    if prov == 'cashapp':
        h = _CASHAPP_RE.search(subj) or _CASHAPP_RE.search(body)
        if h:
            return h.group(0).lstrip('$')
        m = re.search(r"^\s*([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})\s+sent\s+you\s+\$?\d", subj, re.I)
        if m:
            return m.group(1).strip()
        m2 = re.search(r"\b([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})\b.*sent\s+you\s+\$?\d", body, re.I)
        if m2:
            return m2.group(1).strip()
        # Cash App body pattern with amount followed by sender name.
        m3 = re.search(r"you were sent\s+\$?[\d.,]+\s+by\s+([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})", body, re.I)
        if m3:
            return m3.group(1).strip()
    if prov == 'paypal':
        m = re.search(r"Note from\s+([^\n<]+)", body)
        if m:
            return m.group(1)
        m2 = re.search(r"([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,2})", subj)
        if m2:
            return m2.group(1)
    #Generic fallbacks when provider-specific patterns fail
    m = re.search(r"\b([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})\b\s+sent\s+you\s+\$?\d", subj, re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"\b([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})\b\s+paid\s+you\b", subj, re.I)
    if m2:
        return m2.group(1).strip()
    # Fallback Cash App body pattern with amount followed by sender name.
    m3 = re.search(r"you were sent\s+\$?[\d.,]+\s+by\s+([A-Z][A-Za-z'`\-]+(?:\s+[A-Z][A-Za-z'`\-]+){0,3})", body, re.I)
    if m3:
        return m3.group(1).strip()
    return None

_RESOLVED_SHEET_USERNAME_TO_UID: Dict[str, int] = {}

#--- Debug helper ---

def _debug(name: str, trigger: str = "", output: str = ""):
    try:
        log_event({"event": "dues_debug", "name": name, "trigger": trigger, "output": output})
    except Exception:
        pass

#--- Officer tokens ---

def _officer_name_tokens(bot) -> set[str]:
    toks: set[str] = set()
    try:
        officer_ids = set(officer_role_ids(settings))
        if not officer_ids:
            return toks
        for g in getattr(bot, 'guilds', []) or []:
            for member in getattr(g, 'members', []) or []:
                try:
                    roles = getattr(member, 'roles', []) or []
                    if not any(int(getattr(r, 'id', 0) or 0) in officer_ids for r in roles):
                        continue
                except Exception:
                    continue
                nm = getattr(getattr(member, 'name', None), 'lower', lambda: "")() or (str(getattr(member,'name',''))).lower()
                dn = (getattr(member, 'display_name', '') or '').lower()
                for s in (nm, dn):
                    if not s: continue
                    toks.add(s)
                    first = s.split()[0]
                    if first: toks.add(first)
    except Exception:
        pass
    return toks

#============================
#Core scoring model (blueprint)
#============================

#Tunable weights (can move to settings later)
_W = {
    'sheet': {
        'discord_handle_overlap': 0.50,
        'name_overlap': 0.70,
        'provider_in_sheet': 0.20,
        'provider_in_dues': 0.10,
    },
    'email': {
        'provider_match': 0.50,
        'provider_mismatch': -0.15,
        'time_2h': 0.45,
        'time_24h': 0.35,
        'time_120h': 0.20,
        'name_overlap': 0.60,
        'amount_15': 0.25,
        'amount_tiers': 0.10,
    },
    'bonus': {
        'provider_consistency': 0.10,
    },
    'thresholds': {
        'strong': 1.0,
        'review': 0.6,
    }
}

_ALLOWED_AMOUNTS = set(int(x) for x in getattr(settings,'dues_allowed_amounts',[15,20,25,30]))

def _time_score(dts: Optional[datetime], ets: Optional[datetime]) -> float:
    if not dts or not ets:
        return 0.0
    delta_h = abs((ets - dts).total_seconds()) / 3600.0
    if delta_h <= 2: return _W['email']['time_2h']
    if delta_h <= 24: return _W['email']['time_24h']
    if delta_h <= 120: return _W['email']['time_120h']
    return 0.0

def _score_sheet(D: dict, S: dict) -> float:
    s = 0.0
    au = (_norm_user(D.get('author_name')))
    ad = (_norm_user(D.get('author_display')))
    sd = _norm_user(S.get('discord_username'))
    if sd and (sd in au or sd in ad or au in sd or ad in sd):
        s += _W['sheet']['discord_handle_overlap']
    if D.get('name') and S.get('full_name'):
        s += _W['sheet']['name_overlap'] * _jaccard_tokens(S['full_name'], D['name'])
    ph = D.get('provider')
    paid_where = (S.get('paid_where') or '').lower()
    kind = (S.get('kind') or '').lower()
    if ph and ph in paid_where:
        s += _W['sheet']['provider_in_sheet']
    if ph and ph in kind:
        s += _W['sheet']['provider_in_dues']
    return s

def _score_email(D: dict, S: Optional[dict], E: dict, ets: Optional[datetime]) -> float:
    s = 0.0
    ph = D.get('provider')
    if ph and E['provider'] == ph:
        s += _W['email']['provider_match']
    elif ph and E['provider'] and E['provider'] != ph:
        s += _W['email']['provider_mismatch']
    s += _time_score(D.get('ts'), ets)
    name_pool: List[str] = []
    if D.get('name'): name_pool.append(D['name'])
    if S and S.get('full_name'): name_pool.append(S['full_name'])
    if E.get('payer_name') and name_pool:
        s += _W['email']['name_overlap'] * max(_jaccard_tokens(E['payer_name'], n) for n in name_pool)
    amt = E.get('amount')
    if amt is not None:
        if int(round(amt)) == 15: s += _W['email']['amount_15']
        elif int(round(amt)) in {20,25,30}: s += _W['email']['amount_tiers']
    return s

#============================
#Portal fetch & main analysis
#============================

async def _fetch_portal_messages(bot, include_processed: bool = False, limit_override: Optional[int] = None) -> list:
    ch_id = getattr(settings, 'ch_due_portal', None)
    if not ch_id:
        log_action('dues_portal', 'missing_channel', '')
        return []
    ch = bot.get_channel(int(ch_id))
    if not ch or not hasattr(ch, 'history'):
        log_action('dues_portal', 'channel_not_found', str(ch_id))
        return []
    msgs = []
    conf_limit = int(getattr(settings,'dues_scan_limit',0) or 0)
    #Bound history read to keep the command snappy. If DUES_SCAN_LIMIT=0, default to 500.
    if limit_override is not None and int(limit_override) > 0:
        limit = int(limit_override)
    else:
        limit = conf_limit if conf_limit > 0 else 500
    try:
        #Fetch newest-first then reverse to chronological for downstream logic
        fetched = []
        async for m in ch.history(limit=limit, oldest_first=False):
            fetched.append(m)
        msgs = list(reversed(fetched))
    except Exception as e:
        log_action('dues_portal_history_error', f'ch={ch_id}', str(e))
        return []
    if include_processed:
        return msgs
    #Skip messages already marked as processed (reaction fallback when delete fails)
    processed_ids = _load_dues_index_ids()
    processed = []
    for m in msgs:
        try:
            mid = str(getattr(m, 'id', '') or '')
            if mid and mid in processed_ids:
                continue
        except Exception:
            pass
        try:
            reactions = getattr(m, 'reactions', []) or []
            if any(str(r.emoji) == _DUES_PROCESSED_EMOJI and getattr(r, 'me', False) for r in reactions):
                continue
        except Exception:
            pass
        processed.append(m)
    return processed

#---- main ----

async def _analyze_dues(bot) -> List[dict]:
    _debug('begin')
    msgs = await _fetch_portal_messages(bot)
    members = _load_membership_rows()

    #Filter portal messages to explicit payment statements
    parsed_msgs: List[Tuple[Any, dict]] = []
    for m in msgs:
        p = _parse_portal_message(m)
        if not _is_explicit_payment_message(p.get('content','')):
            continue
        parsed_msgs.append((m, p))

    if not parsed_msgs:
        return []
    #Optionally skip a few oldest messages, but only when there are plenty of payments
    skip = max(0, int(getattr(settings,'dues_scan_skip_oldest',3) or 0))
    if skip and len(parsed_msgs) > max(20, skip):
        parsed_msgs = parsed_msgs[skip:]

    #Restrict member rows by earliest message timestamp (with buffer)
    oldest_ts = parsed_msgs[0][1]['ts']
    min_date = (oldest_ts.replace(tzinfo=None) - timedelta(days=int(getattr(settings,'dues_email_window_days',3) or 3))).date()
    def _parse_date(s: str):
        try:
            return datetime.fromisoformat(s.replace('Z','+00:00')).date()
        except Exception:
            return None
    members = [r for r in members if (not r.get('date')) or (_parse_date(r.get('date')) is None) or (_parse_date(r.get('date')) >= min_date)]

    #Preload emails in the global window
    start = parsed_msgs[0][1]['ts'].replace(tzinfo=None) - timedelta(days=int(getattr(settings,'dues_email_window_days',5) or 5))
    end = parsed_msgs[-1][1]['ts'].replace(tzinfo=None) + timedelta(days=int(getattr(settings,'dues_email_window_days',5) or 5))
    raw_emails = _load_email_logs_between(start, end)

    #Prepare emails with features
    prepped_emails: List[dict] = []
    for e in raw_emails:
        subj, body, frm = (e.get('subject','') or ''), (e.get('content','') or ''), (e.get('from','') or '')
        provider = _provider_from_email(frm, subj, body) or ''
        text = subj + ' ' + body
        amount = _extract_amount(text)
        payer_name = _payment_username_from_email({'subject': subj, 'content': body, 'from': frm}) or ''
        ts_utc = None
        try:
            ts_utc = datetime.fromisoformat((e.get('ts_received') or e.get('ts_logged')).replace('Z','+00:00'))
        except Exception:
            ts_utc = None
        prepped_emails.append({
            'id': str(e.get('id') or ''),
            'provider': provider,
            'amount': amount,
            'payer_name': payer_name,
            'ts_utc': ts_utc,
            'raw': e,
        })

    #Build member indexes
    by_discord: Dict[str, dict] = {}
    by_handle: Dict[str, List[dict]] = {}
    names_vocab: List[Tuple[str, dict]] = []
    def _add_handle_map(h: str, row: dict):
        key = h.lstrip('@$').lower()
        if key:
            by_handle.setdefault(key, []).append(row)
    for mm in members:
        dn = _norm_user(mm.get('discord_username'))
        if dn:
            by_discord[dn] = mm
        pu = mm.get('payment_username') or ''
        for h in _handles_from_text(pu):
            _add_handle_map(h, mm)
        for tok in re.findall(r"[@$][A-Za-z][A-Za-z0-9_\-]{1,31}", pu):
            _add_handle_map(tok, mm)
        fn = (mm.get('full_name') or '').strip()
        if fn:
            names_vocab.append((fn, mm))

    #Candidate store for email uniqueness enforcement
    per_msg_candidates: Dict[int, dict] = {}
    email_to_candidates: Dict[str, List[Tuple[float, int]]] = {}

    #Pre-map membership rows to actual guild members (optional heavy step)
    member_to_rows: Dict[int, List[Tuple[dict, int]]] = {}
    if not getattr(settings, 'dues_fast_map', True):
        guild, guild_members = await _guild_and_members(bot)
        if guild and guild_members:
            m_matched, m_possible, _m_unmatched = _match_membership_rows_to_members(members, guild_members)
            for rec in m_matched:
                mid = int(getattr(rec['member'], 'id', 0) or 0)
                if not mid:
                    continue
                member_to_rows.setdefault(mid, []).append((rec['row'], int(rec['score'])))

    for m, p in parsed_msgs:
        #Member candidates (membership rows)
        mem_candidates: List[dict] = []
        #Prefer rows mapped to this Discord author (robust guild-based matching)
        try:
            aid = int(getattr(getattr(m, 'author', None), 'id', 0) or 0)
        except Exception:
            aid = 0
        if aid and aid in member_to_rows:
            #take top few by pre-match score
            mem_candidates = [r for r,_sc in sorted(member_to_rows[aid], key=lambda x: -x[1])][:10]
        handles_norm = [h.lstrip('@$').lower() for h in (p.get('handles') or [])]
        an = _norm_user(p.get('author_name'))
        if not mem_candidates and an and an in by_discord:
            mem_candidates = [by_discord[an]]
        if not mem_candidates and handles_norm:
            seen = set()
            for h in handles_norm:
                for mm in by_handle.get(h, []) or []:
                    tid = id(mm)
                    if tid not in seen:
                        seen.add(tid); mem_candidates.append(mm)
        if not mem_candidates and p.get('name'):
            cand = p['name']
            scored = []
            for fn, mm in names_vocab:
                r = _name_match(cand, fn)
                if r >= 75:
                    scored.append((r, mm))
            scored.sort(reverse=True)
            mem_candidates = [mm for r, mm in scored[:10]]
        if not mem_candidates and an and len(an) >= 6:
            scored = []
            for mm in members:
                dn = _norm_user(mm.get('discord_username'))
                if not dn: continue
                #Containment match: if one is substring of the other (min 6 chars to avoid false positives)
                if len(dn) >= 6 and (an in dn or dn in an):
                    scored.append((95, mm))
                else:
                    r = _ratio(an, dn)
                    if r >= 85:
                        scored.append((r, mm))
            scored.sort(reverse=True)
            mem_candidates = [mm for r, mm in scored[:10]]
        if not mem_candidates:
            mem_candidates = members[:50]

        #Score sheet candidates
        scored_S: List[Tuple[float, dict]] = []
        for S in mem_candidates:
            sc = _score_sheet(p, S)
            if sc > 0:
                scored_S.append((sc, S))
        scored_S.sort(key=lambda x: x[0], reverse=True)
        S_best = scored_S[0][1] if scored_S else None
        S_best_score = scored_S[0][0] if scored_S else 0.0

        #Email candidates in time window
        email_cands: List[Tuple[float, dict]] = []
        for E in prepped_emails:
            ets = E['ts_utc']
            if not ets:
                continue
            #quick time window prefilter ±5d already applied globally, so accept all
            se = _score_email(p, S_best, E, ets)
            if se > 0:
                email_cands.append((se, E))
        email_cands.sort(key=lambda x: x[0], reverse=True)
        E_best = email_cands[0][1] if email_cands else None
        E_best_score = email_cands[0][0] if email_cands else 0.0

        total = S_best_score + E_best_score
        if E_best and p.get('provider') and E_best['provider'] == p.get('provider'):
            total += _W['bonus']['provider_consistency']

        msg_id = int(getattr(m,'id',0) or 0)
        per_msg_candidates[msg_id] = {
            'discord': p,
            'message': m,
            'sheet_best': S_best,
            'sheet_score': S_best_score,
            'email_best': E_best,
            'email_score': E_best_score,
            'total_score': total,
            'email_cands': email_cands[:8],  #top few for uniqueness pass
        }
        #Collect for uniqueness enforcement
        for sc, E in email_cands[:8]:
            if E['id']:
                email_to_candidates.setdefault(E['id'], []).append((sc, msg_id))

    #Enforce email uniqueness: for each email id, keep the highest-scoring message
    best_msg_for_email: Dict[str, int] = {}
    for eid, lst in email_to_candidates.items():
        lst.sort(key=lambda x: x[0], reverse=True)
        best_msg_for_email[eid] = lst[0][1]

    #Finalize records with uniqueness applied
    results: List[dict] = []
    used_emails: set[str] = set()
    for msg_id, rec in per_msg_candidates.items():
        p = rec['discord']
        S = rec['sheet_best']
        E = rec['email_best']
        E_final = None
        if E and E['id']:
            if best_msg_for_email.get(E['id']) == msg_id and E['id'] not in used_emails:
                E_final = E
                used_emails.add(E['id'])
        #Build reasons
        reasons: List[str] = []
        if S:
            if p.get('name') and S.get('full_name'):
                reasons.append(f"name_overlap_sheet↔discord={_jaccard_tokens(S['full_name'], p['name']):.2f}")
            au = _norm_user(p.get('author_name')); ad = _norm_user(p.get('author_display')); sd = _norm_user(S.get('discord_username'))
            if sd and (sd in au or sd in ad):
                reasons.append('discord_handle_overlap')
            if p.get('provider') and p['provider'] in (S.get('paid_where','').lower()):
                reasons.append('provider_in_sheet')
        if E_final:
            if E_final['provider']:
                reasons.append(f"email_provider={E_final['provider']}")
            if E_final['payer_name']:
                pool = []
                if p.get('name'): pool.append(p['name'])
                if S and S.get('full_name'): pool.append(S['full_name'])
                if pool:
                    reasons.append(f"name_overlap_email↔pool={max(_jaccard_tokens(E_final['payer_name'], n) for n in pool):.2f}")
            if p.get('ts') and E_final.get('ts_utc'):
                delta_h = abs((E_final['ts_utc'] - p['ts']).total_seconds())/3600.0
                reasons.append(f"timing_delta_h={delta_h:.2f}")
            if E_final.get('amount') is not None:
                reasons.append(f"email_amount={E_final['amount']:.2f}")

        #Flags for cash/donation-in-kind (relaxed when we have a matching provider email)
        flag_reason = None
        if S:
            paid_where = (S.get('paid_where') or '').lower()
            kind = (S.get('kind') or '').lower()
            #Avoid false positives on 'cash app'
            if re.search(r"\bcash\b(?!\s*app)", paid_where) or re.search(r"\bin\s*person\b", paid_where):
                flag_reason = 'cash'
            elif 'donat' in kind and 'dues' not in kind:
                if 'verif' not in kind:
                    flag_reason = 'donation'
        #If we have a solid provider email and typical dues amount, clear flags
        if rec.get('email_best') or E_final:
            eprov = (E_final or {}).get('provider') if E_final else (rec.get('email_best') or {}).get('provider')
            eamt = (E_final or {}).get('amount') if E_final else (rec.get('email_best') or {}).get('amount')
            subj = ((E_final or {}).get('raw') or {}).get('subject', '') if (E_final or rec.get('email_best')) else ''
            if eprov in {'venmo','cashapp','paypal'} and (eamt is None or int(round(eamt or 0)) in _ALLOWED_AMOUNTS or 'dues' in subj.lower()):
                flag_reason = None

        results.append({
            'event': 'dues_portal_analysis',
            'message_id': msg_id,
            'ts': p.get('ts').isoformat() if isinstance(p.get('ts'), datetime) else str(p.get('ts')),
            'author': p.get('author_name'),
            'author_display': p.get('author_display'),
            'content': p.get('content'),
            'provider': p.get('provider'),
            'handles': p.get('handles'),
            'name_guess': p.get('name'),
            'primary_member': S or {},
            'primary_email': (E_final['raw'] if E_final else None),
            'payment_username_email': _payment_username_from_email(E_final['raw']) if E_final else None,
            'payment_username_sheet': S.get('payment_username') if S else None,
            'flag_reason': flag_reason,
            'score_sheet': float(rec['sheet_score']),
            'score_email': float(rec['email_score'] if E_final else 0.0),
            'score_total': float(rec['sheet_score'] + (rec['email_score'] if E_final else 0.0)),
            'reasons': reasons,
        })

        try:
            await _append_dues_log(results[-1])
        except Exception:
            pass

    return results

#============================
#Public command: handle_check_dues (updated presentation, same signature)
#============================

async def handle_check_dues(intent, ctx) -> None:
    """Show a quick dues status summary for a given member."""
    if not getattr(settings, 'dues_enabled', True):
        await safe_send(ctx['channel'], 'Dues checking is disabled in settings.')
        return
    bot = ctx.get('bot')
    if not bot:
        await safe_send(ctx['channel'], 'Bot context missing.')
        return
    placeholder = None
    try:
        placeholder = await ctx['channel'].send('Analyzing dues…')
    except Exception:
        await safe_send(ctx['channel'], 'Analyzing dues…')
        placeholder = None
    try:
        rows = await _analyze_dues(bot)
        if not rows:
            if placeholder:
                await placeholder.edit(content='No portal messages to analyze.')
            else:
                await safe_send(ctx['channel'], 'No portal messages to analyze.')
            return
        def _conf_label(score: float) -> str:
            if score >= 1.20: return 'high confidence'
            if score >= 0.90: return 'medium-high'
            if score >= 0.60: return 'review'
            return 'low'
        lines = []
        for rec in rows[-15:]:
            best_mem = rec.get('primary_member') or {}
            best_email = rec.get('primary_email')
            auth = rec.get('author') or ''
            disp = rec.get('author_display') or auth
            #Only hard-flag if we have no corroborating provider email attached
            if rec.get('flag_reason') in {'cash','donation'} and not best_email:
                fn = best_mem.get('full_name') or 'No associated form entry found'
                lines.append(f"- Discord: {auth} ({disp}), Real Name: {fn}, Payment App Username: —, Score = FLAGGED FOR REVIEW")
                continue
            if not best_mem:
                lines.append(f"- Discord: {auth} ({disp}), Real Name: No associated form entry found, Payment App Username: —, Score = 0 (No match found)")
                continue
            name = best_mem.get('full_name') or 'No associated form entry found'
            pay_from_email = rec.get('payment_username_email') or '—'
            score = float(rec.get('score_total') or 0.0)
            lines.append(f"- Discord: {auth} ({disp}), Real Name: {name}, Payment App Username: {pay_from_email}, Score = {score:.2f} ({_conf_label(score)})")
        header = "Recent dues check results:\n"
        body = "\n".join(lines[:15])
        max_len = 1900
        chunks: List[str] = []
        cur = ""
        for line in body.split("\n"):
            nxt = (cur + ("\n" if cur else "") + line)
            if len(nxt) > max_len:
                if cur:
                    chunks.append(cur)
                cur = line
                if len(cur) > max_len:
                    while len(cur) > max_len:
                        chunks.append(cur[:max_len])
                        cur = cur[max_len:]
            else:
                cur = nxt
        if cur:
            chunks.append(cur)
        if placeholder:
            await placeholder.edit(content=header + (chunks[0] if chunks else ""))
        else:
            await safe_send(ctx['channel'], header + (chunks[0] if chunks else ""))
        for i in range(1, len(chunks)):
            await safe_send(ctx['channel'], chunks[i])
    except Exception as e:
        if placeholder:
            try:
                await placeholder.edit(content=f"Dues error: {e}")
            except Exception:
                await safe_send(ctx['channel'], f"Dues error: {e}")
        else:
            await safe_send(ctx['channel'], f"Dues error: {e}")
        log_action('dues_check_error', '', str(e))

#============================
#Dues scheduler - Daily verification and role sync
#============================

def _get_uninvited_uta_emails(semester: str) -> list[str]:
    """Return UTA emails for the semester that haven't been MavOrgs invited."""
    rows = _load_membership_rows()
    cur_sem_norm = _norm_sem_label(semester).lower()
    
    uninvited = []
    for row in rows:
        if not row.get('verified'):
            continue
        if row.get('mavorgs_invite'):
            continue
        
        sem_norm = _norm_sem_label(row.get('semester', '')).lower()
        if sem_norm != cur_sem_norm:
            continue
        
        email = (row.get('email') or '').strip().lower()
        if email.endswith('@mavs.uta.edu') or email.endswith('@uta.edu'):
            uninvited.append(email)
    
    return sorted(set(uninvited))

async def _sync_dues_roles(bot, guild, cur_sem: str, today_date) -> tuple[list, list]:
    """Add roles to verified members, remove from expired.
    
    Returns (added_members, removed_members) for logging.
    
    Role addition: Uses simple username matching (same as before).
    Role removal: Uses robust matching to avoid removing roles from users who 
                 changed their Discord username but still have valid dues.
    """
    from datetime import date
    role_id = int(getattr(settings, 'role_due_paying_id', 0) or 0)
    if not role_id:
        role_id = int(getattr(settings, 'role_dues_perks_id', 0) or 0)
    if not role_id:
        return [], []
    
    role_obj = guild.get_role(role_id)
    if not role_obj:
        log_action('dues_role_sync', 'role_not_found', f'id={role_id}')
        return [], []
    
    #Load all membership rows
    rows = _load_membership_rows()
    membership_source = _MEMBERSHIP_ROWS_LAST_SOURCE
    membership_authoritative = _MEMBERSHIP_ROWS_LAST_AUTHORITATIVE
    membership_error = _MEMBERSHIP_ROWS_LAST_ERROR
    
    #Build set of valid handles for members with non-expired, verified dues
    valid_handle_keys: set[str] = set()
    for row in rows:
        if not row.get('verified'):
            continue
        sem_label = row.get('semester', '')
        expiry = _get_semester_expiry(sem_label)
        if today_date > expiry:
            continue
        raw_handle = (row.get('discord_username') or '').strip()
        if not raw_handle:
            continue
        for cand in _expand_handle_variants(_split_handle_candidates(raw_handle) or [raw_handle]):
            key = _norm_user_key(cand)
            if key:
                valid_handle_keys.add(key)
    try:
        log_action(
            'dues_role_sync_valid',
            f'handles={len(valid_handle_keys)}',
            f'source={membership_source}; authoritative={int(membership_authoritative)}'
        )
    except Exception:
        pass

    if not membership_authoritative and not valid_handle_keys:
        try:
            detail = f'source={membership_source}'
            if membership_error:
                detail += f'; error={membership_error}'
            log_action('dues_role_sync_abort', 'membership_unavailable', detail)
        except Exception:
            pass
        return [], []
    
    added = []
    removed = []
    allow_removals = membership_authoritative
    if not allow_removals:
        try:
            detail = f'source={membership_source}'
            if membership_error:
                detail += f'; error={membership_error}'
            log_action('dues_role_sync_mode', 'add_only', detail)
        except Exception:
            pass
    
    #Get full member list (avoid partial cache so role removals aren't skipped)
    members_list = await _ensure_guild_members(guild, force_fetch=True)
    try:
        log_action('dues_role_sync_members', f'count={len(members_list)}', 'ok')
    except Exception:
        pass
    
    def _member_keys(member) -> set[str]:
        keys: set[str] = set()
        raw_names: list[str] = []
        try:
            raw_names.extend([
                getattr(member, 'name', '') or '',
                getattr(member, 'display_name', '') or '',
                getattr(member, 'global_name', '') or '',
            ])
        except Exception:
            raw_names = []
        for raw in raw_names:
            if not raw:
                continue
            for cand in _expand_handle_variants([raw]):
                k = _norm_user_key(cand)
                if k:
                    keys.add(k)
        return keys

    role_holder_count = 0
    valid_role_holder_count = 0

    #Iterate guild members
    for member in members_list:
        has_role = role_obj in member.roles
        member_keys = _member_keys(member)
        member_has_valid_dues = bool(member_keys & valid_handle_keys)
        if not member_has_valid_dues and member_keys and valid_handle_keys:
            #Allow very small handle drift (1 edit) for reasonably long keys
            for mk in member_keys:
                if len(mk) < 6:
                    continue
                for vk in valid_handle_keys:
                    if len(vk) < 6:
                        continue
                    if _edit_distance(mk, vk) <= 1:
                        member_has_valid_dues = True
                        break
                if member_has_valid_dues:
                    break
        if has_role:
            role_holder_count += 1
            if member_has_valid_dues:
                valid_role_holder_count += 1
        
        #=== ROLE ADDITION ===
        if member_has_valid_dues and not has_role:
            try:
                await member.add_roles(role_obj, reason="TomCat: verified dues for current semester")
                added.append(member)
            except Exception as e:
                log_action('dues_role_add_error', f'uid={member.id}', str(e))
        
        #=== ROLE REMOVAL ===
        elif has_role and not member_has_valid_dues:
            if not allow_removals:
                continue
            try:
                await member.remove_roles(role_obj, reason="TomCat: dues expired, not renewed")
                removed.append(member)
            except Exception as e:
                log_action('dues_role_remove_error', f'uid={member.id}', str(e))
    
    try:
        log_action(
            'dues_role_sync_stats',
            f'role_holders={role_holder_count} valid={valid_role_holder_count}',
            f'members={len(members_list)}; source={membership_source}; authoritative={int(membership_authoritative)}'
        )
    except Exception:
        pass
    return added, removed


async def _run_daily_dues_job(bot) -> None:
    """Execute the daily dues verification and role sync."""
    from datetime import date
    
    #Get target guild
    guild = None
    guild_id = int(getattr(settings, 'target_guild_id', 0) or 0)
    if guild_id:
        guild = bot.get_guild(guild_id)
    if not guild:
        for g in getattr(bot, 'guilds', []):
            guild = g
            break
    if not guild:
        log_action('dues_scheduler', 'no_guild', 'skipped')
        return
    
    #Get channels
    log_ch_id = int(getattr(settings, 'ch_logging', 0) or 0)
    member_ch_id = int(getattr(settings, 'ch_member_names', 0) or 0)
    log_ch = bot.get_channel(log_ch_id) if log_ch_id else None
    member_ch = bot.get_channel(member_ch_id) if member_ch_id else None
    
    cur_sem = _current_semester_label()
    today_date = date.today()
    
    #1. Analyze dues portal messages
    try:
        rows = await _analyze_dues(bot)
    except Exception as e:
        log_action('dues_scheduler_analyze_error', '', str(e))
        rows = []
    
    #2. Auto-verify high-confidence entries (score >= 0.90)
    threshold = float(getattr(settings, 'dues_auto_verify_threshold', 0.90) or 0.90)
    verified = []
    for rec in rows:
        score = float(rec.get('score_total', 0.0) or 0.0)
        if score >= threshold:
            verified.append(rec)
            #Log full breakdown to machine logs
            log_action('dues_auto_verify', 
                       f"user={rec.get('author','?')} score={score:.2f}",
                       json.dumps({k: v for k, v in rec.items() if k.startswith('score_') or k in ['author', 'provider', 'semester']}))
    
    #3. Mark verified in sheet if we have emails
    try:
        emails_to_verify: list[tuple[str, str]] = []
        if verified:
            emails_to_verify.extend([
                (
                    (r.get('primary_member') or {}).get('email', '').strip().lower(),
                    (r.get('primary_member') or {}).get('semester') or r.get('semester') or cur_sem
                )
                for r in verified
                if (r.get('primary_member') or {}).get('email')
            ])
        #Add fallback email-only matches so portal messages are not required
        rows_for_fallback: list[dict] = []
        extra: list[tuple[str, str]] = []
        try:
            rows_for_fallback = _load_membership_rows()
            extra = _email_only_candidates(rows_for_fallback, cur_sem)
            if extra:
                emails_to_verify.extend(extra)
        except Exception:
            pass
        #Deduplicate
        seen = set()
        deduped: list[tuple[str, str]] = []
        for e, s in emails_to_verify:
            if not e:
                continue
            key = (e, s or '')
            if key in seen:
                continue
            seen.add(key)
            deduped.append((e, s))
        emails_to_verify = deduped
        if emails_to_verify:
            ok, msg = await _mark_verified_emails(emails_to_verify)
            if ok:
                _MEMBERSHIP_ROWS_CACHE = None
                _MEMBERSHIP_ROWS_TS = 0.0
        if extra:
            try:
                deleted_fallback = await _cleanup_portal_messages_for_emails(bot, rows_for_fallback, extra)
                if deleted_fallback:
                    log_action('dues_scheduler_cleanup', f'fallback_deleted={deleted_fallback}', '')
            except Exception:
                pass
        try:
            verified_cleanup = await _cleanup_portal_messages_for_verified_rows(bot, _load_membership_rows(), cur_sem)
            if verified_cleanup:
                log_action('dues_scheduler_cleanup', f'verified_deleted={verified_cleanup}', '')
        except Exception:
            pass
    except Exception as e:
        log_action('dues_mark_verified_error', '', str(e))
    
    #3b. Delete portal messages for verified entries
    if verified:
        try:
            ids = [int(r.get('message_id') or 0) for r in verified if int(r.get('message_id') or 0)]
            if ids:
                deleted = await _delete_portal_messages(bot, ids)
                log_action('dues_scheduler_cleanup', f'deleted={deleted}', '')
        except Exception as e:
            log_action('dues_portal_delete_error', '', str(e))
    
    #4. Log to CH_LOGGING (human readable)
    if log_ch and verified:
        lines = ["Dues processed and verified:"]
        for rec in verified:
            uname = rec.get('author', 'unknown')
            provider = rec.get('provider', 'unknown')
            score = float(rec.get('score_total', 0.0) or 0.0)
            lines.append(f"  {uname} ({provider}) - score {score:.2f}")
        try:
            await safe_send(log_ch, '\n'.join(lines))
        except Exception as e:
            log_action('dues_log_send_error', 'verified_list', str(e))
    
    #5. Sync roles (add for verified, remove for expired)
    added, removed = await _sync_dues_roles(bot, guild, cur_sem, today_date)
    
    if log_ch and (added or removed):
        def _names(members: list) -> list[str]:
            out: list[str] = []
            for m in members:
                try:
                    name = getattr(m, 'name', '') or getattr(m, 'display_name', '') or str(getattr(m, 'id', ''))
                    if name:
                        out.append(name)
                except Exception:
                    continue
            #De-dupe, keep stable sort for readability
            return sorted({n.strip() for n in out if n.strip()}, key=lambda s: s.lower())

        role_lines = []
        if added:
            added_names = _names(added)
            role_lines.append(f"Roles added: {len(added_names)}")
            role_lines.extend([f"-{n}" for n in added_names])
        if removed:
            removed_names = _names(removed)
            role_lines.append(f"Roles removed (expired): {len(removed_names)}")
            role_lines.extend([f"-{n}" for n in removed_names])
        try:
            await safe_send(log_ch, '\n'.join(role_lines))
        except Exception:
            pass

    #5b. Report notable users without dues (feeding schedule + officers)
    if log_ch:
        try:
            #Resolve dues role
            role_id = int(getattr(settings, 'role_due_paying_id', 0) or 0)
            if not role_id:
                role_id = int(getattr(settings, 'role_dues_perks_id', 0) or 0)
            role_obj = guild.get_role(role_id) if role_id else None

            #Build member maps
            members_list = await _ensure_guild_members(guild, force_fetch=False)
            id_to_member = {}
            key_to_member = {}
            for m in members_list:
                try:
                    mid = int(getattr(m, 'id', 0) or 0)
                    if mid:
                        id_to_member[mid] = m
                except Exception:
                    pass
                try:
                    raw_names = [
                        getattr(m, 'name', '') or '',
                        getattr(m, 'display_name', '') or '',
                        getattr(m, 'global_name', '') or '',
                    ]
                except Exception:
                    raw_names = []
                for raw in raw_names:
                    if not raw:
                        continue
                    for cand in _expand_handle_variants([raw]):
                        k = _norm_user_key(cand)
                        if k and k not in key_to_member:
                            key_to_member[k] = m

            def _member_name(m) -> str:
                try:
                    return getattr(m, 'name', '') or getattr(m, 'display_name', '') or str(getattr(m, 'id', ''))
                except Exception:
                    return str(getattr(m, 'id', 'unknown'))

            def _has_dues(m) -> bool:
                if not m or not role_obj:
                    return False
                try:
                    return role_obj in getattr(m, 'roles', [])
                except Exception:
                    return False

            #Feeding schedule for current week
            sched_no_dues: list[str] = []
            try:
                from .feeding import _resolve_schedule_for_date, _coerce_uid
                resolved = _resolve_schedule_for_date(today_date)
                sched = resolved.get("schedule", {}) or {}
                assignee_ids = []
                for seq in sched.values():
                    if not isinstance(seq, list):
                        continue
                    for raw in seq:
                        uid = _coerce_uid(raw)
                        if uid:
                            assignee_ids.append(uid)
                assignee_ids = list(dict.fromkeys(assignee_ids))

                for uid in assignee_ids:
                    member = None
                    if isinstance(uid, int):
                        member = id_to_member.get(uid) or guild.get_member(uid)
                    else:
                        key = _norm_user_key(str(uid))
                        member = key_to_member.get(key) if key else None
                    if member and not _has_dues(member):
                        sched_no_dues.append(_member_name(member))
            except Exception:
                pass

            #Officers without dues
            officers_no_dues: list[str] = []
            try:
                officer_ids = set(officer_role_ids(settings))
                seen_member_ids: set[int] = set()
                for off_id in officer_ids:
                    off_role = guild.get_role(int(off_id)) if off_id else None
                    if not off_role:
                        continue
                    for m in list(getattr(off_role, 'members', []) or []):
                        mid = int(getattr(m, "id", 0) or 0)
                        if mid and mid in seen_member_ids:
                            continue
                        if mid:
                            seen_member_ids.add(mid)
                        if not _has_dues(m):
                            officers_no_dues.append(_member_name(m))
            except Exception:
                pass

            #Format message
            sched_no_dues = sorted({n.strip() for n in sched_no_dues if n.strip()}, key=lambda s: s.lower())
            officers_no_dues = sorted({n.strip() for n in officers_no_dues if n.strip()}, key=lambda s: s.lower())

            if sched_no_dues or officers_no_dues:
                lines = ["Notable users without dues:"]
                if sched_no_dues:
                    lines.append(f"Feeding schedule (current week): {len(sched_no_dues)}")
                    lines.extend([f"-{n}" for n in sched_no_dues])
                if officers_no_dues:
                    lines.append(f"Officers: {len(officers_no_dues)}")
                    lines.extend([f"-{n}" for n in officers_no_dues])
            else:
                lines = ["Notable users without dues: none"]
            await safe_send(log_ch, "\n".join(lines))
        except Exception as e:
            try:
                log_action('dues_notable_no_dues_error', '', str(e))
            except Exception:
                pass
    
    #6. Output MavOrgs invite list (UTA emails only)
    uninvited = _get_uninvited_uta_emails(cur_sem)
    if log_ch and uninvited:
        email_list = '\n'.join(uninvited)
        view = InvitesConfirmView(0, uninvited)  #0 = any officer can confirm
        try:
            await safe_send(log_ch, f"UTA emails not yet invited to MavOrgs:\n{email_list}", view=view)
        except Exception as e:
            log_action('dues_mavorgs_list_error', '', str(e))
    
    #7. Output to CH_MEMBER_NAMES
    if member_ch and verified:
        for rec in verified:
            real_name = rec.get('full_name') or rec.get('primary_member', {}).get('full_name', 'Unknown')
            username = rec.get('author', 'unknown')
            sem = rec.get('semester') or cur_sem
            line = f"{real_name}, {username}, {_norm_sem_label(sem).lower()}"
            try:
                await safe_send(member_ch, line)
                await asyncio.sleep(1.1)  #Rate limit
            except Exception:
                pass
    
    log_action('dues_scheduler', f'verified={len(verified)} added={len(added)} removed={len(removed)}', 'done')

async def start_dues_scheduler(bot) -> None:
    """Daily dues verification: score-based verification, role sync, MavOrgs invite tracking."""
    if not getattr(settings, 'dues_enabled', True):
        return

    global _DUES_SCHEDULER_STARTED, _LAST_DUES_RUN_KEY
    async with _DUES_SCHEDULER_LOCK:
        if _DUES_SCHEDULER_STARTED:
            log_action('dues_scheduler', 'already_started', 'skipped')
            return
        _DUES_SCHEDULER_STARTED = True
    
    target_h, target_m = 3, 0  #3:00 AM daily
    
    while True:
        #Sleep until target time
        now = _dues_now()
        nxt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        
        try:
            run_key = _dues_run_key()
            if _LAST_DUES_RUN_KEY == run_key:
                log_action('dues_scheduler', f'date={run_key}', 'duplicate_skip')
                continue
            if not _acquire_dues_lock(run_key):
                log_action('dues_scheduler', f'date={run_key}', 'duplicate_skip_lock')
                _LAST_DUES_RUN_KEY = run_key
                continue
            _LAST_DUES_RUN_KEY = run_key
            await _run_daily_dues_job(bot)
        except Exception as e:
            log_action('dues_scheduler_error', '', str(e))

def _dues_month_path(dt: datetime) -> str:
    mon = _MONTH_NAMES.get(dt.month, f"{dt.month:02d}")
    return os.path.join(DUES_DIR, f"{dt.year}-{mon}.ndjson")

def _dues_month_paths_between(start_dt: datetime, end_dt: datetime) -> list[str]:
    start_dt = _to_utc_naive(start_dt)
    end_dt = _to_utc_naive(end_dt)
    files: list[str] = []
    seen: set[str] = set()
    cur = datetime(start_dt.year, start_dt.month, 1)
    while cur <= end_dt:
        p = _dues_month_path(cur)
        if p not in seen:
            files.append(p)
            seen.add(p)
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1)
        else:
            cur = datetime(cur.year, cur.month + 1, 1)
    return files

def _dues_log_message_ids_for_emails(emails_with_sem: list[tuple[str, str | None]]) -> list[int]:
    """Look up portal message IDs from dues logs for the provided emails/semester."""
    emails_with_sem = [((e or '').strip().lower(), _norm_sem_label(s or '') if s else '') for e, s in emails_with_sem if e]
    if not emails_with_sem:
        return []
    lookup: dict[str, set[str]] = {}
    for e, s in emails_with_sem:
        lookup.setdefault(e, set()).add(s or '')
    backfill_days = int(getattr(settings, 'dues_cleanup_log_backfill_days', 120) or 120)
    now = _dues_now()
    start = now - timedelta(days=backfill_days)
    end = now + timedelta(days=1)
    paths = _dues_month_paths_between(start, end)
    best: dict[tuple[str, str], tuple[float, datetime, int]] = {}
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get('event') != 'dues_portal_analysis':
                        continue
                    pm = obj.get('primary_member') or {}
                    email = (pm.get('email') or '').strip().lower()
                    if not email or email not in lookup:
                        continue
                    sem = _norm_sem_label(pm.get('semester') or '')
                    want = lookup.get(email) or {''}
                    if want and '' not in want and sem and sem not in want:
                        continue
                    mid = int(obj.get('message_id') or 0)
                    if not mid:
                        continue
                    score = float(obj.get('score_total') or 0.0)
                    ts_raw = obj.get('ts') or ''
                    try:
                        ts = datetime.fromisoformat(str(ts_raw).replace('Z', '+00:00'))
                    except Exception:
                        ts = now
                    key = (email, sem or '')
                    prev = best.get(key)
                    if prev is None or (score > prev[0]) or (score == prev[0] and ts > prev[1]):
                        best[key] = (score, ts, mid)
        except Exception:
            continue
    ids = [v[2] for v in best.values()]
    return list(dict.fromkeys(ids))

async def _append_dues_log(row: dict):
    ts = row.get('ts')
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(timezone.utc)

    mid = str(row.get('message_id') or '')
    try:
        existing = set()
        if os.path.exists(DUES_INDEX):
            with open(DUES_INDEX, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        if obj.get('message_id'):
                            existing.add(str(obj['message_id']))
                    except Exception:
                        continue

        path = _dues_month_path(dt)
        if mid and mid in existing:
            #If monthly file was deleted but index remains, re-write the row
            if not os.path.exists(path):
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return

        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

        if mid:
            with open(DUES_INDEX, 'a', encoding='utf-8') as f:
                f.write(json.dumps({"message_id": mid, "ts": _now_iso()}) + "\n")
    except Exception:
        pass


