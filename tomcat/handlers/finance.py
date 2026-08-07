"""Non-dues finance ingestion: classify emails, build Sheets payloads, notify sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple, Set

from ..config import settings
from ..logger import log_action
from ..services.sheets_client import sheets_client
from ..utils.datetime_utils import format_mmddyyyy
from ..utils.payments import detect_provider
from ..utils.sender import safe_send

FINANCE_DIR = os.path.join("logs", "finance")
os.makedirs(FINANCE_DIR, exist_ok=True)
FINANCE_INDEX = os.path.join(FINANCE_DIR, "index.jsonl")
FINANCE_LOCK = asyncio.Lock()

_EMAIL_LOGS_DIR = os.path.join("logs", "emails")
_DUES_INDEX_PATH = os.path.join("logs", "dues", "index.jsonl")
_RESOLVED_DUES_FILE = os.path.join(FINANCE_DIR, "resolved_dues_emails.jsonl")

_DONATION_DEFAULT = "Donations"
_INCOME_TYPES = {
    "foods_goods": "Foods/Goods fundraisers",
    "other": "Other Fundraisers",
    "donations": "Donations",
    "adoption": "Adoption Fees",
}
_EXPENSE_TYPES = {
    "vet": "Vet Bills",
    "food": "Food",
    "storage": "Storage Unit Fee",
    "website": "Website Fee",
    "supplies": "Supplies",
    "misc": "Misc/Reimbursement",
}

_DUES_AMOUNT = 15.0
_DUES_TOL = 0.75
_PENDING_DUES_FILE = os.path.join(FINANCE_DIR, "pending_dues.jsonl")
_PENDING_DUES_HOLD_DAYS = 3

FOODS_GOODS_KEYWORDS = {
    "bake", "bakesale", "bake sale", "cookie", "cookies", "brownie", "brownies",
    "cupcake", "cupcakes", "cake", "cakes", "scone", "banana bread", "lemonade",
    "drink", "drinks", "dr pepper", "soda", "snack", "snacks", "dessert",
    "food", "foods", "goods", "pastry", "pastries", "chai", "coffee", "tea",
    "candy", "pretzel", "dirt cup", "rice krisp", "treat", "sweet treat"
}
OTHER_FUNDRAISER_KEYWORDS = {
    "sticker", "stickers", "merch", "shirt", "shirts", "hoodie", "hoodies",
    "sweater", "pin", "pins", "button", "buttons", "keychain", "keychains",
    "crochet", "plush", "plushie", "bookmark", "bracelet", "earring", "earrings",
    "redbubble", "etsy", "table fee", "vendor fee", "activity fair"
}
ADOPTION_KEYWORDS = {
    "adoption", "adopt", "adopting", "adoption fee", "adopt fee", "adopted",
    "rehoming fee",
}
#Words that positively mark a payment as a donation rather than a purchase.
#Checked before the circumstantial signals in _score_fundraiser_signal, so an
#explicit "donation" on a busy bake-sale day is still filed as a donation.
DONATION_KEYWORDS = {
    "donation", "donate", "donating", "donated", "deposit", "gofundme",
    "approved by", "treasurer", "vet bill", "fundraiser for",
}
DEDUCTION_WORDS = {"dues", "membership", "member", "due"}

_DUES_MESSAGE_IDS: Set[str] = set()

VET_KEYWORDS = {
    "vet", "veterinary", "clinic", "vaccine", "vaccination", "spay", "neuter",
    "appointment", "banfield", "animal hospital", "exam", "surgery", "meds", "medicine",
    "tcap", "flea", "prevention", "treatment"
}
FOOD_EXPENSE_KEYWORDS = {
    "petco", "petsmart", "pet smart", "chewy", "cat food", "food", "litter", "kibble",
    "treat", "treats", "wet food", "dry food", "purina", "friskies", "royal canin",
    "walmart", "kroger", "heb", "costco", "sams", "sam's", "tractor supply",
    "temptations", "fancy feast",
}
STORAGE_KEYWORDS = {
    "py store", "storage unit", "storage fee", "ps store here", "ps store", "store here"
}
WEBSITE_KEYWORDS = {
    "wix", "domain", "website", "site fee", "hosting", "catsofuta.org", "squarespace",
}
SUPPLIES_KEYWORDS = {
    "supply", "supplies", "poster", "flyer", "table", "banner", "balloon", "cups",
    "plates", "sign", "marker", "ink", "toner", "printing", "tape", "scissors",
    "decor", "decoration", "craft", "glue", "string", "paint", "brush", "bag",
    "trap", "traps", "transfer cage", "carrier", "paper towels", "bleach", "gloves",
    "zip tie", "zip ties", "home depot", "lowes", "hardware"
}

_CURRENCY_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)")
_RECENT_ROWS_LIMIT = 1000

#Sheet rows carry the Gmail id that produced them. The local index (index.jsonl)
#is only a cache: it was lost in the Hetzner migration, which blinded every
#id-based dedup check and let a bulk re-log append ~400 duplicate rows. The
#sheet is the one store that survives a host move, so identity lives there too.
_BOT_MARKER = "[Recorded by the TomCat bot"
_BOT_TAG_RE = re.compile(r"\btc:([A-Za-z0-9_\-]{4,})")

#Expense categories billed by a single recurring vendor. For these the category
#already identifies the payee, so an officer naming the service ("storage unit
#fee") and the bot naming the merchant ("Py Store Here Pantego") are the same
#payment even though the text never matches.
_SINGLE_VENDOR_TYPES = {"storage unit fee", "website fee"}

#Vendor bills post on a statement date but often get logged by hand a day or two
#later. These charges recur monthly, so a few days of slack cannot collide with
#the next bill.
_VENDOR_DAY_WINDOW = 3


def _bot_marker(email_id: str) -> str:
    """Build the sheet suffix that marks a row as bot-written."""
    eid = (email_id or "").strip()
    if not eid:
        return f" {_BOT_MARKER}]"
    return f" {_BOT_MARKER} | tc:{eid}]"


def _sheet_email_id(name_field: str) -> str:
    """Recover the Gmail id embedded in a bot-written sheet row, if present."""
    m = _BOT_TAG_RE.search(name_field or "")
    return m.group(1) if m else ""

_TXN_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"Transaction ID[:\s#]*([A-Z0-9\-]{10,})",
        r"Receipt ID[:\s#]*([A-Z0-9\-]{8,})",
        r"Payment ID[:\s#]*([A-Z0-9\-]{10,})",
        r"Authorization ID[:\s#]*([A-Z0-9\-]{10,})",
        r"\bID[:\s#]*([A-Z0-9]{10,})",
        r"cash\.app/(?:launch/activity/)?receipt/([A-Z0-9=_\-]{12,})",
        r"cash\.app/(?:receipt/)?payments/([A-Z0-9\-]{8,})/receipt",
    )
]


def _find_currency_amount(*texts: str) -> Optional[float]:
    """Return the last currency amount found across provided text snippets."""
    for text in texts:
        if not text:
            continue
        matches = _CURRENCY_RE.findall(text)
        if matches:
            amt = matches[-1]
            try:
                return float(amt.replace(',', ''))
            except Exception:
                continue
    return None


def _coerce_amount(raw: str, *fallback_texts: str) -> float:
    """Prefer dollar-prefixed matches from context; fall back to raw digits."""
    amt = _find_currency_amount(*fallback_texts)
    if amt is not None:
        return amt
    try:
        return float((raw or '').replace(',', ''))
    except Exception:
        return 0.0


def _extract_txn_id(*texts: str) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        for pat in _TXN_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(1).strip()
    return None

PROVIDER_NAMES = {
    "paypal": "Paypal",
    "venmo": "Venmo",
    "cashapp": "Cashapp",
}

MESSAGE_EMPTY_SENTINEL = "(no message)"


@dataclass
class FinanceEvent:
    """Structured view of a finance email after classification."""
    email_id: str
    provider: str
    counterparty: str
    note: str
    amount: float
    direction: str  #"income" or "expense"
    category: Optional[str]
    ts: datetime
    raw_subject: str
    raw_content: str
    message_blank: bool
    message_id: Optional[str] = None
    txn_id: Optional[str] = None
    provider_ts: Optional[datetime] = None
    #Set by _assign_income_category: "keyword" | "high" | "low". "low" means the
    #category is a guess and an officer should confirm it, rather than the old
    #behaviour of silently defaulting to Donations.
    category_confidence: str = ""
    category_reason: str = ""

    @property
    def payment_type(self) -> str:
        """Pretty provider label for Sheets output."""
        return PROVIDER_NAMES.get(self.provider, self.provider.title())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint_hash_from_value(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint_hash_for_event(ev: "FinanceEvent") -> str:
    return _fingerprint_hash_from_value(_fingerprint(ev))


def _compact_index_record(record: dict) -> Optional[dict]:
    email_id = str(record.get("email_id") or "").strip()
    status = str(record.get("status") or "").strip().lower()
    fingerprint_hash = str(record.get("fingerprint_hash") or "").strip()
    if not fingerprint_hash:
        fingerprint_hash = _fingerprint_hash_from_value(record.get("fingerprint") or "")
    if not email_id or status not in {"income", "expense"} or not fingerprint_hash:
        return None

    compact = {
        "email_id": email_id,
        "status": status,
        "fingerprint_hash": fingerprint_hash,
        "ts": record.get("ts") or _now_iso(),
    }
    for key in ("provider", "message_id", "txn_id", "provider_ts"):
        value = record.get(key)
        if value not in (None, ""):
            compact[key] = value
    return compact


def _load_index() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not os.path.exists(FINANCE_INDEX):
        return out
    keep: List[dict] = []
    changed = False
    try:
        with open(FINANCE_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    compact = _compact_index_record(obj)
                    if compact:
                        out[compact["email_id"]] = compact
                        keep.append(compact)
                        if compact != obj:
                            changed = True
                    else:
                        changed = True
                except Exception:
                    changed = True
                    continue
    except Exception:
        return out
    if changed:
        try:
            with open(FINANCE_INDEX, "w", encoding="utf-8") as f:
                for obj in keep:
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return out


def _append_index(record: dict) -> None:
    compact = _compact_index_record(record)
    if not compact:
        return
    try:
        with open(FINANCE_INDEX, "a", encoding="utf-8") as f:
            f.write(json.dumps(compact, ensure_ascii=False) + "\n")
    except Exception as e:
        log_action("finance_index_write_error", f"email_id={compact.get('email_id')}", str(e))


def reconcile_index_from_sheets() -> Tuple[int, int]:
    """Rebuild missing index entries from tc: ids already present in the sheets.

    index.jsonl is a cache; the sheets are the durable record. After a host
    migration or a fresh deploy the index can be far smaller than the number of
    bot-written rows, which silently disables every id-based dedup check. This
    restores the email_id set from the sheets so that gap cannot reopen.

    Rows written before the tc: tag shipped carry no id and cannot be recovered
    here — those stay covered by the sheet-snapshot check in _looks_duplicate.

    Returns (scanned_tagged_rows, restored_records).
    """
    sid = getattr(settings, 'sheet_megasheet_id', None)
    if not sid:
        return (0, 0)
    known = set(_load_index().keys())
    scanned = 0
    restored = 0
    try:
        book = sheets_client().open_by_key(sid)
    except Exception as e:
        log_action('finance_index_rebuild_error', 'open', str(e))
        return (0, 0)

    targets = (
        ('income', getattr(settings, 'income_ws_title', 'Income')),
        ('expense', getattr(settings, 'expense_ws_title', 'Expenses')),
    )
    for kind, ws_name in targets:
        try:
            vals = book.worksheet(ws_name).get_all_values()
        except Exception as e:
            log_action('finance_index_rebuild_error', f'{kind}_fetch', str(e))
            continue
        for rec in _parse_sheet_records(vals, 'income' if kind == 'income' else 'expense'):
            eid = rec.get('sheet_email_id')
            if not eid:
                continue
            scanned += 1
            if eid in known:
                continue
            #The row's own fingerprint is unrecoverable from the sheet, so we
            #synthesize a stable one. Only the email_id check relies on this
            #record, and that check runs first.
            _append_index({
                "email_id": eid,
                "status": kind,
                "fingerprint_hash": _fingerprint_hash_from_value(f"sheet|{kind}|{eid}"),
                "ts": _now_iso(),
            })
            known.add(eid)
            restored += 1

    if restored:
        log_action('finance_index_rebuilt', f'scanned={scanned}', f'restored={restored}')
    return (scanned, restored)


def _index_settled(ev: "FinanceEvent", reason: str) -> None:
    """Record an email as settled so it is never re-evaluated.

    Confirmed duplicates were previously left out of the index, which meant
    every scan re-classified them and re-read both worksheets to prove the same
    thing again. After the migration wiped the index that was 130 events on
    every run, forever. A payment already present in the sheet is as finished as
    one we just wrote.
    """
    try:
        _append_index({
            "email_id": ev.email_id,
            "status": ev.direction,
            "provider": ev.provider,
            "fingerprint_hash": _fingerprint_hash_for_event(ev),
            "message_id": ev.message_id,
            "txn_id": ev.txn_id,
            "provider_ts": (ev.provider_ts or ev.ts).isoformat(),
            "settled_as": reason,
        })
    except Exception as e:
        log_action("finance_index_write_error", f"settled={ev.email_id}", str(e))


def _load_fingerprints() -> Set[str]:
    """Return set of hashed fingerprints for previously logged transactions."""
    fps: Set[str] = set()
    if not os.path.exists(FINANCE_INDEX):
        return fps
    try:
        with open(FINANCE_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                fp = obj.get("fingerprint_hash")
                if not fp:
                    fp = _fingerprint_hash_from_value(obj.get("fingerprint") or "")
                if isinstance(fp, str) and fp:
                    fps.add(fp)
    except Exception:
        return fps
    return fps


def _load_txn_ids() -> Set[str]:
    txn: Set[str] = set()
    if not os.path.exists(FINANCE_INDEX):
        return txn
    try:
        with open(FINANCE_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                tx = obj.get("txn_id")
                if isinstance(tx, str) and tx:
                    txn.add(tx)
    except Exception:
        return txn
    return txn


def _load_message_ids() -> Set[str]:
    mids: Set[str] = set()
    if not os.path.exists(FINANCE_INDEX):
        return mids
    try:
        with open(FINANCE_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                mid = obj.get("message_id")
                if isinstance(mid, str) and mid:
                    mids.add(mid)
    except Exception:
        return mids
    return mids


def _iter_email_logs() -> Iterable[dict]:
    """Yield logged email payloads newest-first across monthly ndjson files."""
    if not os.path.exists(_EMAIL_LOGS_DIR):
        return []
    files = [
        os.path.join(_EMAIL_LOGS_DIR, name)
        for name in os.listdir(_EMAIL_LOGS_DIR)
        if name.endswith(".ndjson")
    ]
    files.sort()
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except Exception:
            continue


def _iter_email_logs_newest_first() -> Iterable[dict]:
    """Yield logged email payloads newest-first by reading newest files in reverse."""
    if not os.path.exists(_EMAIL_LOGS_DIR):
        return []
    files = [
        os.path.join(_EMAIL_LOGS_DIR, name)
        for name in os.listdir(_EMAIL_LOGS_DIR)
        if name.endswith(".ndjson")
    ]
    files.sort(reverse=True)
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _load_dues_message_ids() -> Set[str]:
    """Return identifiers already consumed by the dues pipeline.

    Historical dues logs stored Discord portal `message_id` values in the index,
    while monthly dues analysis logs also contain the Gmail `primary_email.id`.
    Finance needs the Gmail ids to avoid reprocessing those payment emails.
    """
    ids: Set[str] = set()
    if not os.path.exists(_DUES_INDEX_PATH):
        pass
    else:
        try:
            with open(_DUES_INDEX_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    mid = obj.get("message_id")
                    if isinstance(mid, str) and mid:
                        ids.add(mid)
        except Exception:
            return ids
    dues_dir = os.path.dirname(_DUES_INDEX_PATH)
    if os.path.isdir(dues_dir):
        try:
            for name in os.listdir(dues_dir):
                if not name.endswith(".ndjson") or name == os.path.basename(_DUES_INDEX_PATH):
                    continue
                path = os.path.join(dues_dir, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except Exception:
                                continue
                            email_obj = obj.get("primary_email") or {}
                            email_id = email_obj.get("id")
                            if isinstance(email_id, str) and email_id:
                                ids.add(email_id)
                except Exception:
                    continue
        except Exception:
            return ids
    return ids


def _load_resolved_dues_email_ids() -> Set[str]:
    ids: Set[str] = set()
    if not os.path.exists(_RESOLVED_DUES_FILE):
        return ids
    try:
        with open(_RESOLVED_DUES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                eid = obj.get("email_id")
                if isinstance(eid, str) and eid:
                    ids.add(eid)
    except Exception:
        return ids
    return ids


def _append_resolved_dues_email_id(email_id: str, reason: str) -> None:
    if not email_id:
        return
    try:
        with open(_RESOLVED_DUES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "email_id": email_id,
                "reason": reason,
                "ts": _now_iso(),
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        log_action("pending_dues_resolve_write_error", f"email_id={email_id}", str(e))


def _clean_counterparty(name: str) -> str:
    """Normalize sender/payee strings pulled from email subjects."""
    return name.strip().strip('.')


def _is_likely_dues(amount: Optional[float], text: str) -> bool:
    """Heuristic guardrail so finance only logs true non-dues entries."""
    text_low = text.lower()
    if any(word in text_low for word in DEDUCTION_WORDS):
        return True
    #Note: $15 payments without keywords are NOT automatically dues.
    #They require corroborating evidence (dues portal message or membership form).
    #The dues pipeline handles that cross-referencing; finance should log as income
    #unless explicit keywords are present.
    return False


def _ensure_dues_ids() -> None:
    if not _DUES_MESSAGE_IDS:
        _DUES_MESSAGE_IDS.update(_load_dues_message_ids())


def _is_dues_email(amount: Optional[float], text: str, message_id: Optional[str]) -> bool:
    if message_id:
        _ensure_dues_ids()
        if message_id in _DUES_MESSAGE_IDS:
            return True
    return _is_likely_dues(amount, text)


def _is_potential_dues_amount(amount: Optional[float]) -> bool:
    """Check if amount is close to $15 (dues amount)."""
    if amount is None:
        return False
    return abs(amount - _DUES_AMOUNT) <= _DUES_TOL


def _load_pending_dues() -> List[dict]:
    """Load pending dues payments from file."""
    if not os.path.exists(_PENDING_DUES_FILE):
        return []
    records = []
    try:
        with open(_PENDING_DUES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return records


def _save_pending_dues(records: List[dict]) -> None:
    """Save pending dues payments to file."""
    try:
        with open(_PENDING_DUES_FILE, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:
        log_action("pending_dues_save_error", "", str(e))


def _add_pending_due(event: "FinanceEvent") -> None:
    """Add a $15 payment to pending dues for later corroboration check."""
    from datetime import timedelta
    if event.email_id in _load_resolved_dues_email_ids():
        return
    records = _load_pending_dues()
    #Skip if already pending
    if any(r.get("email_id") == event.email_id for r in records):
        return
    expires = (event.ts + timedelta(days=_PENDING_DUES_HOLD_DAYS)).isoformat()
    record = {
        "email_id": event.email_id,
        "counterparty": event.counterparty,
        "amount": event.amount,
        "provider": event.provider,
        "note": event.note,
        "ts": event.ts.isoformat(),
        "expires": expires,
        "raw_subject": event.raw_subject,
        "raw_content": event.raw_content[:500] if event.raw_content else "",
    }
    records.append(record)
    _save_pending_dues(records)
    log_action("pending_dues_add", f"counterparty={event.counterparty}", f"expires={expires}")


def _remove_pending_due(email_id: str, reason: str = "") -> None:
    """Remove a pending due by email_id."""
    records = _load_pending_dues()
    new_records = [r for r in records if r.get("email_id") != email_id]
    if len(new_records) < len(records):
        _save_pending_dues(new_records)
        log_action("pending_dues_remove", f"email_id={email_id}", reason)


def _get_pending_due_ids() -> Set[str]:
    """Return set of email_ids currently in pending dues."""
    return {r.get("email_id") for r in _load_pending_dues() if r.get("email_id")}




def _extract_note(text: str) -> str:
    """Trim boilerplate prefixes and whitespace from payment notes."""
    if not text:
        return ""
    text = text.strip()
    if text.lower().startswith("for "):
        text = text[3:].strip()
    if text.lower().startswith("note:"):
        text = text[5:].strip()
    return text.strip()


def _looks_amount_like(s: str) -> bool:
    s = (s or "").strip().replace(",", "")
    if not s:
        return False
    allowed = set("0123456789$.-")
    if not set(s) <= allowed:
        return False
    return any(ch.isdigit() for ch in s)


def _extract_note_hints_from_field(field: str) -> Tuple[str, str]:
    """Parse counterparty and note from a sheet-formatted name field."""
    base = field or ""
    rec_idx = base.find("[Recorded")
    if rec_idx != -1:
        base = base[:rec_idx].strip()
    note = ""
    m = re.search(r"\(Message:\s*(.*?)\)\s*$", base)
    if m:
        note = m.group(1).strip()
        # "none" is the sentinel the bot writes for blank notes — treat as empty
        if note.lower() == "none":
            note = ""
        base = base[:m.start()].strip()
    return base.strip(), note


def _extract_venmo_note(subject: str, body: str, existing: Optional[str] = None) -> str:
    candidates: List[str] = []
    if existing:
        candidates.append(existing)
    
    # Venmo subjects usually include the payer and amount inline.
    # Venmo bodies often split the payer name, dollar sign, amount, and note
    # across separate lines.
    #.
    #00
    #
    #Note Here
    #See transaction

    if body:
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        try:
            for idx, line in enumerate(lines):
                if "see transaction" in line.lower():
                    #The note is typically the line immediately preceding "See transaction"
                    #UNLESS that line is the amount.
                    prev = lines[idx-1]
                    if not _looks_amount_like(prev) and "paid you" not in prev.lower() and "credited to" not in prev.lower():
                         candidates.append(prev)
                    break
        except Exception:
            pass

    for cand in candidates:
        cleaned = _extract_note(cand)
        if cleaned and not _looks_amount_like(cleaned):
            return cleaned
    return ""


def _extract_cashapp_note(subject: str, body: str, existing: Optional[str] = None) -> str:
    candidates: List[str] = []
    if existing:
        candidates.append(existing)
    
    # Cash App subjects often include payer, amount, and note in one line.
    m = re.search(r"sent you \$[0-9.,]+\s+for\s+(.+)", subject or "", re.I)
    if m:
        candidates.insert(0, m.group(1)) #Top priority
    
    body_patterns = [
        r"(?:Note|Message)\s*:\s*(.+)",
        r"For\s+(.+)",
    ]
    if body:
        for pat in body_patterns:
            m2 = re.search(pat, body, re.I)
            if m2:
                candidates.append(m2.group(1))
                break
    
    for cand in candidates:
        cleaned = _extract_note(cand)
        if cleaned:
            cleaned = re.split(
                r"(?:,\s*approved by .*|\.\s*to view your receipt.*|\s+to view your receipt.*|\s+to report a problem.*)",
                cleaned,
                maxsplit=1,
                flags=re.I,
            )[0].strip(" .")
        if cleaned and not cleaned.lower().startswith("for any") and "view your receipt" not in cleaned.lower():
            return cleaned
    return ""


def _extract_paypal_note(subject: str, body: str, existing: Optional[str] = None) -> str:
    candidates: List[str] = []
    if existing:
        candidates.append(existing)
    
    #PayPal Body: "Note from [Name] \n [Note]"
    if body:
        #Try multi-line match first for "Note from X \n content"
        m_multi = re.search(r"Note(?:\s+from\s+.*?)?\s*\n\s*\n\s*(.+?)\n", body, re.I | re.DOTALL)
        if m_multi:
             candidates.append(m_multi.group(1))

        #Fallback patterns
        body_patterns = [
            r"Note(?:\s+from\s+(?:buyer|customer|.*?))?\s*:\s*(.+)",
            r"Message\s*:\s*(.+)",
        ]
        for pat in body_patterns:
            m = re.search(pat, body, re.I)
            if m:
                candidates.append(m.group(1))
    
    for cand in candidates:
        cleaned = _extract_note(cand)
        if cleaned:
            return cleaned
    return ""


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def _fingerprint(ev: "FinanceEvent") -> str:
    #Use specific time to distinguish repeat purchases on the same day
    ts = ev.provider_ts or ev.ts
    #Use minute-precision timestamp so 12:00 and 12:05 are distinct
    time_str = ts.strftime("%Y-%m-%dT%H:%M")
    
    #Include email_id and cleaned note in fingerprint for uniqueness
    base = "|".join([
        ev.email_id,
        ev.direction,
        time_str,
        f"{ev.amount:.2f}",
        _norm_text(ev.counterparty),
        _norm_text(ev.note),
        ev.provider,
    ])
    return base

#--- Venmo-specific parsing of payment notifications ---

def _classify_venmo(email: dict) -> Tuple[Optional[FinanceEvent], str]:
    subject = email.get("subject", "")
    content = email.get("content", "")
    text = subject.strip()
    body = content
    ts = _parse_timestamp(email)
    message_id = email.get("message_id")
    txn_id = _extract_txn_id(subject, content)

    # Match person-to-person payments received.
    m = re.match(r"^(?P<name>.+?)\s+(?:paid|sent)\s+you\s+\$?(?P<amount>[0-9.,]+)(?:\s+for\s+(?P<note>.+))?", text, re.I)
    if m:
        name = _clean_counterparty(m.group("name"))
        amount = _coerce_amount(m.group("amount"), subject, body)
        note = _extract_venmo_note(subject, body, m.group("note"))
        note = _extract_note(note)
        blank = not bool(note.strip())
        if _is_dues_email(amount, f"{subject} {note}", message_id):
            return (None, "dues")
        category = _categorize_income(note or subject, subject)
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="venmo",
            counterparty=name,
            note=note,
            amount=amount,
            direction="income",
            category=category,
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=blank,
            message_id=message_id,
            txn_id=txn_id,
        ), "income")

    # Match person-to-person payments sent.
    m = re.match(r"^you\s+paid\s+(?P<name>.+?)\s+\$?(?P<amount>[0-9.,]+)(?:\s+for\s+(?P<note>.+))?", text, re.I)
    if m:
        name = _clean_counterparty(m.group("name"))
        amount = _coerce_amount(m.group("amount"), subject, body)
        note = _extract_venmo_note(subject, body, m.group("note"))
        note = _extract_note(note)
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="venmo",
            counterparty=name,
            note=note,
            amount=amount,
            direction="expense",
            category=_categorize_expense(name, note or subject),
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=not bool(note.strip()),
            message_id=message_id,
            txn_id=txn_id,
        ), "expense")

    return (None, "ignore")



#--- Cash App parsing logic mirrors Venmo but handles spending receipts ---

def _classify_cashapp(email: dict) -> Tuple[Optional[FinanceEvent], str]:
    subject = email.get("subject", "")
    content = email.get("content", "")
    text = subject.strip()
    body = content
    ts = _parse_timestamp(email)
    message_id = email.get("message_id")
    txn_id = _extract_txn_id(subject, content)

    m = re.match(r"^(?P<name>.+?)\s+(?:paid|sent)\s+you\s+\$?(?P<amount>[0-9.,]+)(?:\s+for\s+(?P<note>.+))?", text, re.I)
    if not m:
        m = re.search(r"You\s+were\s+sent\s+\$?(?P<amount>[0-9.,]+)\s+by\s+(?P<name>.+?)(?:[.]\s*|\s+for\s+(?P<note>.+))", body, re.I)
    if m:
        groups = m.groupdict()
        name = _clean_counterparty(groups.get("name") or "")
        amount = _coerce_amount(groups.get("amount") or "", subject, body)
        note = _extract_cashapp_note(subject, body, groups.get("note"))
        note = _extract_note(note)
        blank = not bool(note.strip())
        if _is_dues_email(amount, f"{subject} {note}", message_id):
            return (None, "dues")
        category = _categorize_income(note or subject, subject)
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="cashapp",
            counterparty=name,
            note=note,
            amount=amount,
            direction="income",
            category=category,
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=blank,
            message_id=message_id,
            txn_id=txn_id,
        ), "income")

    # Match Cash App transfers sent from the account holder.
    m = re.match(r"^You sent \$?(?P<amount>[0-9.,]+)\s+to\s+(?P<name>.+?)(?:\s+for\s+(?P<note>.+))?$", text, re.I)
    if not m:
        m = re.search(
            r"You\s+(?:paid\s+(?P<name_paid>.+?)\s+\$?(?P<amount_paid>[0-9.,]+)|sent\s+\$?(?P<amount_sent>[0-9.,]+)\s+to\s+(?P<name_sent>.+?))(?:\s+for\s+(?P<note>.+))?(?:[.,]|$)",
            body,
            re.I,
        )
    if m:
        groups = m.groupdict()
        name = _clean_counterparty(groups.get("name") or groups.get("name_paid") or groups.get("name_sent") or "")
        amount = _coerce_amount(groups.get("amount") or groups.get("amount_paid") or groups.get("amount_sent") or "", subject, body)
        note = _extract_cashapp_note(subject, body, groups.get("note"))
        note = _extract_note(note)
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="cashapp",
            counterparty=name,
            note=note,
            amount=amount,
            direction="expense",
            category=_categorize_expense(name, note or subject),
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=not bool(note.strip()),
            message_id=message_id,
            txn_id=txn_id,
        ), "expense")

    #Spending via Cash App (debit card style purchases at merchants)
    m = re.search(r"You spent \$([0-9.,]+)\s+at\s+([^\n]+)", content, re.I)
    if m:
        amount = _coerce_amount(m.group(1), subject, content)
        name = _clean_counterparty(m.group(2))
        note = _extract_cashapp_note(subject, body, None)
        note = _extract_note(note)
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="cashapp",
            counterparty=name,
            note=note,
            amount=amount,
            direction="expense",
            category=_categorize_expense(name, subject),
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=not bool(note.strip()),
            message_id=message_id,
            txn_id=txn_id,
        ), "expense")

    return (None, "ignore")



#--- PayPal notifications: more varied subjects/receipts ---

def _classify_paypal(email: dict) -> Tuple[Optional[FinanceEvent], str]:
    subject = email.get("subject", "")
    content = email.get("content", "")
    text = subject.strip()
    body = content
    ts = _parse_timestamp(email)
    message_id = email.get("message_id")
    txn_id = _extract_txn_id(subject, content)

    if "you've got money" in text.lower() or "money received" in text.lower():
        m = re.search(r"([A-Za-z][A-Za-z '\.-]+)\s+sent\s+you\s+\$([0-9.,]+)", content)
        if not m:
            m = re.search(r"Money received\s+from\s+([^\n]+)\s+\$([0-9.,]+)", content)
        if m:
            name = _clean_counterparty(m.group(1))
            amount = _coerce_amount(m.group(2), subject, content)
            note = _extract_paypal_note(subject, content, None)
            note = _extract_note(note)
            blank = not bool(note.strip())
            if _is_dues_email(amount, f"{subject} {note}", message_id):
                return (None, "dues")
            category = _categorize_income(note or subject, subject) or _DONATION_DEFAULT
            return (FinanceEvent(
                email_id=email.get("id", ""),
                provider="paypal",
                counterparty=name,
                note=note,
                amount=amount,
                direction="income",
                category=category,
                ts=ts,
                raw_subject=subject,
                raw_content=content,
                message_blank=blank,
                message_id=message_id,
                txn_id=txn_id,
            ), "income")

    m = re.search(r"You\s+sent\s+a?\s*\$([0-9.,]+)\s*(?:usd)?\s+payment\s+to\s+([^\n]+)", content, re.I)
    if m:
        amount = _coerce_amount(m.group(1), subject, content)
        name = _clean_counterparty(m.group(2))
        note = _extract_paypal_note(subject, content, None)
        note = _extract_note(note)
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="paypal",
            counterparty=name,
            note=note,
            amount=amount,
            direction="expense",
            category=_categorize_expense(name, note or subject),
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=not bool(note.strip()),
            message_id=message_id,
            txn_id=txn_id,
        ), "expense")

    if re.search(r"statement", text, re.I):
        return (None, "ignore")

    # Fallback for compact "[Name]: $Amount" style lines.
    m = re.match(r"^(?P<name>.+?):\s*\$?(?P<amount>[0-9.,]+)\s*(?:usd)?", text, re.I)
    if not m:
         m = re.match(r"^(?P<name>.+?)\s+sent\s+you\s+\$?(?P<amount>[0-9.,]+)", text, re.I)

    if m:
        name = _clean_counterparty(m.group("name"))
        amount = _coerce_amount(m.group("amount"), subject, body)
        note = _extract_paypal_note(subject, content, None)
        note = _extract_note(note)
        blank = not bool(note.strip())
        if _is_dues_email(amount, f"{subject} {note}", message_id):
            return (None, "dues")
        category = _categorize_income(note or subject, subject)
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="paypal",
            counterparty=name,
            note=note,
            amount=amount,
            direction="income",
            category=category,
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=blank,
            message_id=message_id,
            txn_id=txn_id,
        ), "income")

    return (None, "ignore")


def _parse_timestamp(email: dict) -> datetime:
    ts = email.get("ts_received") or email.get("ts_logged")
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _find_note_in_body(body: str) -> str:
    """Pull venmo/paypal "note: ..." fragments from HTML/plain text."""
    if not body:
        return ""
    m = re.search(r"(?:note|for):\s*(.+)", body, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _categorize_income(text: str, subject_hint: str = "") -> Optional[str]:
    """Map free-form notes to our Income form categories."""
    lower = f"{text or ''} {subject_hint or ''}".lower()
    if any(word in lower for word in ADOPTION_KEYWORDS):
        return _INCOME_TYPES["adoption"]
    if any(word in lower for word in FOODS_GOODS_KEYWORDS):
        return _INCOME_TYPES["foods_goods"]
    if any(word in lower for word in OTHER_FUNDRAISER_KEYWORDS):
        return _INCOME_TYPES["other"]
    if lower.strip():
        # A free-form note with no stronger match is treated as a donation
        # unless the dues pipeline claims it elsewhere.
        pass
    return _INCOME_TYPES["donations"]


def _categorize_expense(counterparty: str, text: str) -> str:
    """Guess the best expense bucket using vendor name + note."""
    lower = f"{counterparty} {text}".lower()
    if any(word in lower for word in VET_KEYWORDS):
        return _EXPENSE_TYPES["vet"]
    if any(word in lower for word in FOOD_EXPENSE_KEYWORDS):
        return _EXPENSE_TYPES["food"]
    if any(word in lower for word in STORAGE_KEYWORDS):
        return _EXPENSE_TYPES["storage"]
    if any(word in lower for word in WEBSITE_KEYWORDS):
        return _EXPENSE_TYPES["website"]
    if any(word in lower for word in SUPPLIES_KEYWORDS):
        return _EXPENSE_TYPES["supplies"]
    return _EXPENSE_TYPES["misc"]


def _build_income_row(event: FinanceEvent) -> List[str]:
    """Translate a FinanceEvent into the Income sheet row schema."""
    #Schema: Timestamp, Month, Year, Email Address, Name, Income type, Amount, Payment Type
    ts = (event.provider_ts or event.ts).astimezone(timezone.utc)
    #Note: Assuming central/local time might be better for Month/Year, 
    #but we'll stick to UTC for consistency unless configured otherwise.
    #For format matching CSV: 10/22/2025
    timestamp = f"{ts.month}/{ts.day}/{ts.year}" 
    month = ts.strftime('%B')
    year = str(ts.year)
    
    name_field = f"{event.counterparty}"
    note = event.note.strip()
    if note:
        name_field += f" (Message: {note})"
    else:
        name_field += " (Message: none)"
    name_field += _bot_marker(event.email_id)

    return [
        timestamp,
        month,
        year,
        "", #Email Address (blank)
        name_field,
        event.category or _DONATION_DEFAULT,
        f"${event.amount:.2f}",
        event.payment_type,
    ]


def _build_expense_row(event: FinanceEvent) -> List[str]:
    """Translate a FinanceEvent into the Expenses sheet row schema."""
    #Schema: Timestamp, Month, Year, Name, Expense Type, Amount
    ts = (event.provider_ts or event.ts).astimezone(timezone.utc)
    timestamp = f"{ts.month}/{ts.day}/{ts.year}"
    month = ts.strftime('%B')
    year = str(ts.year)
    
    name_field = f"{event.counterparty}"
    note = event.note.strip()
    if note:
        name_field += f" (Message: {note})"
    else:
        name_field += " (Message: none)"
    name_field += _bot_marker(event.email_id)

    return [
        timestamp,
        month,
        year,
        name_field,
        event.category or _EXPENSE_TYPES['misc'],
        f"${event.amount:.2f}",
    ]


def _sheet_call_delay() -> float:
    try:
        delay = float(getattr(settings, 'finance_sheet_throttle_sec', 0.0) or 0.0)
    except Exception:
        delay = 0.0
    return delay if delay > 0 else 0.0


async def _throttle_sheet_call() -> None:
    delay = _sheet_call_delay()
    if delay > 0:
        await asyncio.sleep(delay)


def _open_worksheet_with_retry(sid: str, ws_name: str, label: str, attempts: int = 3):
    """Open a worksheet, retrying transient Sheets failures.

    The Sheets API returns a bare 500 often enough that a single failure used to
    fail an entire batch, and because a failed batch is never indexed the same
    emails were reprocessed and re-announced on every later run.
    """
    delay = 1.0
    last: Optional[Exception] = None
    for i in range(max(1, attempts)):
        try:
            return sheets_client().open_by_key(sid).worksheet(ws_name)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
                delay *= 2
    log_action('finance_sheet_error', f'{label}_open',
               f'{type(last).__name__}: {last} (after {attempts} attempts)')
    raise last if last else RuntimeError("worksheet open failed")


def _sheet_row_key(row: List[str]) -> Tuple[str, ...]:
    return tuple((str(cell) if cell is not None else "").strip() for cell in row)


def _recent_sheet_row_counts(ws, label: str, max_rows: int = _RECENT_ROWS_LIMIT) -> Optional[Counter]:
    try:
        vals = ws.get_all_values()
    except Exception as e:
        log_action('finance_sheet_error', f'{label}_verify_fetch', str(e))
        return None
    if not vals:
        return Counter()
    limit = max_rows if max_rows > 0 else _RECENT_ROWS_LIMIT
    rows = vals[-limit:] if len(vals) > limit else vals
    return Counter(_sheet_row_key(row) for row in rows if row)


async def _append_rows_with_retry(ws, rows: List[List[str]], label: str) -> List[Tuple[bool, str]]:
    """Append rows safely, only trusting success once the live sheet confirms it."""
    if not rows:
        return []

    observed_counts = _recent_sheet_row_counts(ws, label)
    results: List[Tuple[bool, str]] = []
    for row in rows:
        row_key = _sheet_row_key(row)
        baseline_count = observed_counts.get(row_key, 0) if observed_counts is not None else None
        delay = 1.0
        success = False
        last_error = "ok"
        try:
            await _throttle_sheet_call()
            ws.append_row(row, value_input_option='USER_ENTERED')
            success = True
            last_error = "ok"
            if observed_counts is not None:
                observed_counts[row_key] = observed_counts.get(row_key, 0) + 1
        except Exception as e:
            last_error = str(e)
            for verify_attempt in range(5):
                if verify_attempt:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, 8.0)
                verified_counts = _recent_sheet_row_counts(ws, label)
                if verified_counts is not None:
                    current_count = verified_counts.get(row_key, 0)
                    previous_count = baseline_count if baseline_count is not None else 0
                    if current_count > previous_count:
                        observed_counts = verified_counts
                        success = True
                        last_error = "ok_verified"
                        log_action('finance_sheet_error', f'{label}_append_verified', str(e))
                        break
                    observed_counts = verified_counts
                    baseline_count = current_count
            if not success:
                last_error = f'append_unverified: {last_error}'
        if not success:
            log_action('finance_sheet_error', f'{label}_append_row', last_error)
        results.append((success, last_error))
    return results


def _fetch_recent_records(ws, kind: str, max_rows: int = _RECENT_ROWS_LIMIT) -> Optional[List[dict]]:
    """Fetch recent rows from a worksheet. Returns None if fetch fails (connection error)."""
    snapshot = _fetch_recent_sheet_snapshot(ws, kind, max_rows=max_rows)
    if snapshot is None:
        return None
    return snapshot[0]


def _parse_sheet_records(rows: List[List[str]], kind: str) -> List[dict]:
    recs: List[dict] = []
    for r in rows:
        try:
            #Normalize columns by schema
            if kind == 'income':
                #[Timestamp, Month, Year, Email, Name, Type, Amount, Payment]
                date_s = (r[0] if len(r) > 0 else '').strip()
                name_field = (r[4] if len(r) > 4 else '').strip()
                income_type = (r[5] if len(r) > 5 else '').strip()
                row_type = income_type
                amount_s = (r[6] if len(r) > 6 else '').strip().replace('$', '')
                provider = (r[7] if len(r) > 7 else '').strip()
            else:
                #[Timestamp, Month, Year, Name, Type, Amount]
                date_s = (r[0] if len(r) > 0 else '').strip()
                name_field = (r[3] if len(r) > 3 else '').strip()
                income_type = ''
                #Expense category, kept separate from income_type so the income
                #category-inference pass at _infer_income_category is unaffected.
                row_type = (r[4] if len(r) > 4 else '').strip()
                amount_s = (r[5] if len(r) > 5 else '').strip().replace('$', '')
                provider = ''

            #Parse date mm/dd/yyyy
            dt = None
            if date_s:
                try:
                    m, d, y = [int(x) for x in date_s.split('/')]
                    dt = datetime(y, m, d).date()
                except Exception:
                    dt = None
            #Parse amount
            try:
                amt = float(amount_s.replace(',', '')) if amount_s else None
            except Exception:
                amt = None
            counterparty, note = _extract_note_hints_from_field(name_field)
            recs.append({
                'date': dt,
                'amount': amt,
                'text': name_field,
                'provider': provider,
                'counterparty': counterparty,
                'note': note,
                'income_type': income_type,
                'row_type': row_type,
                #Substring, not equality: the marker now also carries the tc: id.
                'recorded_by_bot': _BOT_MARKER in name_field,
                'sheet_email_id': _sheet_email_id(name_field),
            })
        except Exception:
            continue
    return recs


def _fetch_recent_sheet_snapshot(ws, kind: str, max_rows: int = _RECENT_ROWS_LIMIT) -> Optional[Tuple[List[dict], Counter]]:
    """Fetch recent rows once so duplicate checks use a live Sheets snapshot."""
    try:
        vals = ws.get_all_values()
    except Exception as e:
        log_action('finance_sheet_error', f'{kind}_fetch', str(e))
        return None

    if not vals:
        return [], Counter()
    limit = max_rows if max_rows > 0 else _RECENT_ROWS_LIMIT
    rows = vals[-limit:] if len(vals) > limit else vals
    return _parse_sheet_records(rows, kind), Counter(_sheet_row_key(row) for row in rows if row)


def _event_as_sheet_record(ev: "FinanceEvent") -> dict:
    return {
        'date': (ev.provider_ts or ev.ts).astimezone(timezone.utc).date(),
        'amount': float(ev.amount),
        'text': "",
        'provider': ev.payment_type if ev.direction == 'income' else '',
        'counterparty': ev.counterparty or '',
        'note': ev.note or '',
        'row_type': ev.category or '',
        'recorded_by_bot': True,
        'sheet_email_id': ev.email_id or '',
    }


def _similar_enough(a: str, b: str) -> bool:
    a = _norm_text(a)
    b = _norm_text(b)
    if not a or not b:
        return False
    try:
        from rapidfuzz import fuzz as rf_fuzz  #type: ignore
        score = rf_fuzz.token_set_ratio(a, b)
        return score >= 82
    except Exception:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _has_refund_word(text: str) -> bool:
    t = _norm_text(text)
    return any(word in t for word in ("refund", "reimburse", "reimburs", "return"))


def _clean_sheet_text(text: str) -> str:
    """Remove [Recorded by...] and other metadata suffixes for cleaner comparison."""
    #Remove [Recorded by the TomCat bot] and similar [brackets] at the end
    #We use a loop to handle nested/multiple brackets if needed, but simple regex is usually enough
    #Pattern: remove any [...] block at the end, possibly repeated
    t = text
    while True:
        prev = t
        t = re.sub(r'\s*\[.*?\]\s*$', '', t)
        t = re.sub(r'\s*\(Message:.*?\)\s*$', '', t) #Also optional: remove (Message: ...) structure if we want base comparison
        if t == prev:
            break
    return t

def _looks_duplicate(ev: "FinanceEvent", recs: List[dict]) -> bool:
    """Sheet fallback duplicate check.

    This is intentionally strict: if note or counterparty differ, treat them as
    different payments even when the amount and day match.
    """
    ev_date = ev.ts.date()
    ev_amt = float(ev.amount)
    ev_counterparty = ev.counterparty or ""
    ev_note = ev.note or ""
    ev_provider = (ev.payment_type or "").strip().lower()

    for r in recs:
        r_date = r.get('date')
        if not r_date:
            continue
        day_gap = abs((r_date - ev_date).days)
        row_type = (r.get('row_type') or '').strip().lower()
        ev_type = (getattr(ev, 'category', '') or '').strip().lower()
        vendor_pair = bool(row_type and row_type == ev_type
                           and row_type in _SINGLE_VENDOR_TYPES)
        if day_gap > (_VENDOR_DAY_WINDOW if vendor_pair else 1):
            continue
        amt = r.get('amount')
        if amt is None:
            continue
        if abs(float(amt) - ev_amt) > 0.01:
            continue

        provider = (r.get('provider') or '').strip().lower()
        if provider and ev_provider and provider != ev_provider:
            continue

        counterparty = r.get('counterparty') or ''
        note = r.get('note') or ''
        counterparty_match = _similar_enough(counterparty, ev_counterparty)

        #A single-vendor recurring charge is identified by its category, not its
        #text: officers write the service ("storage unit fee"), the bot writes
        #the merchant ("Py Store Here Pantego"). Same amount, same category,
        #within the monthly window is the same payment.
        if vendor_pair:
            return True

        #An absent note is missing information, not evidence of a different
        #payment. Compare on the normalized form so that notes which carry no
        #comparable text (an emoji-only note normalizes to "") also count as
        #absent rather than as a mismatch -- _similar_enough returns False for a
        #blank string, which previously vetoed otherwise-identical rows.
        n_sheet = _norm_text(note)
        n_event = _norm_text(ev_note)
        note_match = _similar_enough(n_sheet, n_event) if (n_sheet and n_event) else True

        if counterparty_match and note_match:
            return True
    return False

def _events_maybe_duplicate(a: "FinanceEvent", b: "FinanceEvent") -> bool:
    if a.direction != b.direction:
        return False
    if (a.provider or "").lower() != (b.provider or "").lower():
        return False
    if abs(float(a.amount) - float(b.amount)) > 0.01:
        return False
    ts_a = a.provider_ts or a.ts
    ts_b = b.provider_ts or b.ts
    time_gap = abs((ts_a - ts_b).total_seconds())
    if time_gap > 5 * 60:
        return False
    if not _similar_enough(a.counterparty, b.counterparty):
        return False
    if (a.note or '').strip() or (b.note or '').strip():
        return _similar_enough(a.note, b.note)
    return True


async def _append_income_rows(events: List[FinanceEvent]) -> Dict[str, Tuple[bool, str]]:
    """Flush income events to Sheets and return per-email success info."""
    if not events:
        return {}
    sid = getattr(settings, 'sheet_megasheet_id', None)
    if not sid:
        return {event.email_id: (False, 'missing_sheet_id') for event in events}
    try:
        ws_name = getattr(settings, 'income_ws_title', 'Income')
        ws = _open_worksheet_with_retry(sid, ws_name, 'income')
    except Exception:
        #Distinct reason, not the raw exception text: nothing was written, so
        #the notifier must stay quiet rather than announce a row per payment.
        #The batch is not indexed, so it retries on the next run.
        return {event.email_id: (False, 'sheet_open_failed') for event in events}

    snapshot = _fetch_recent_sheet_snapshot(ws, 'income')
    #If fetch failed (None), we MUST abort to prevent duplicate logging
    if snapshot is None:
        return {event.email_id: (False, 'sheet_read_failed') for event in events}
    existing, existing_row_counts = snapshot

    seen_fp = _load_fingerprints()
    seen_txn = _load_txn_ids()
    seen_msg = _load_message_ids()
    #Union the local index with ids recovered from the sheet itself, so a lost
    #or truncated index.jsonl can no longer resurrect already-logged payments.
    seen_email_ids = set(_load_index().keys())
    seen_email_ids.update(
        r['sheet_email_id'] for r in existing if r.get('sheet_email_id')
    )

    results: Dict[str, Tuple[bool, str]] = {}
    rows: List[List[str]] = []
    idx_events: List[FinanceEvent] = []

    for ev in events:
        # Primary dedup: one email = one payment, never log the same email twice
        if ev.email_id and ev.email_id in seen_email_ids:
            results[ev.email_id] = (False, 'dup_skipped')
            continue

        #Skip known dues just in case classifier missed
        if _is_dues_email(ev.amount, f"{ev.raw_subject} {ev.note}", ev.message_id):
            try:
                log_action('finance_skip_duplicate', 'kind=income_dues', f'{ev.counterparty} ${ev.amount:.2f}')
            except Exception:
                pass
            results[ev.email_id] = (False, 'dues_skip')
            continue

        if ev.txn_id and ev.txn_id in seen_txn:
            try:
                log_action('finance_skip_duplicate', 'income_txn', f'{ev.counterparty} ${ev.amount:.2f}')
            except Exception:
                pass
            results[ev.email_id] = (False, 'dup_skipped')
            continue
        if ev.message_id and ev.message_id in seen_msg:
            try:
                log_action('finance_skip_duplicate', 'income_msg', f'{ev.counterparty} ${ev.amount:.2f}')
            except Exception:
                pass
            results[ev.email_id] = (False, 'dup_skipped')
            continue

        fp_hash = _fingerprint_hash_for_event(ev)
        row = _build_income_row(ev)
        row_key = _sheet_row_key(row)
        if fp_hash in seen_fp or existing_row_counts.get(row_key, 0) > 0 or _looks_duplicate(ev, existing):
            try:
                log_action('finance_skip_duplicate', 'kind=income', f'{ev.counterparty} ${ev.amount:.2f} on {ev.ts.date().isoformat()}')
            except Exception:
                pass
            #Already in the sheet: settle it so later scans skip it outright.
            _index_settled(ev, 'duplicate')
            results[ev.email_id] = (False, 'dup_skipped')
            continue

        rows.append(row)
        idx_events.append(ev)
        seen_fp.add(fp_hash)
        if ev.email_id:
            seen_email_ids.add(ev.email_id)
        if ev.txn_id:
            seen_txn.add(ev.txn_id)
        if ev.message_id:
            seen_msg.add(ev.message_id)
        existing_row_counts[row_key] = existing_row_counts.get(row_key, 0) + 1
        existing.append(_event_as_sheet_record(ev))

    if rows:
        append_results = await _append_rows_with_retry(ws, rows, 'income')
        for ev, (ok, msg) in zip(idx_events, append_results):
            results[ev.email_id] = (ok, msg)
            if ok:
                _append_index({
                    "email_id": ev.email_id,
                    "status": ev.direction,
                    "provider": ev.provider,
                    "fingerprint_hash": _fingerprint_hash_for_event(ev),
                    "message_id": ev.message_id,
                    "txn_id": ev.txn_id,
                    "provider_ts": (ev.provider_ts or ev.ts).isoformat(),
                })
            else:
                log_action('finance_sheet_error', f'income_append_error', msg)

    for ev in events:
        results.setdefault(ev.email_id, (False, 'dup_skipped'))
    return results


async def _append_expense_rows(events: List[FinanceEvent]) -> Dict[str, Tuple[bool, str]]:
    """Flush expense events to Sheets and return per-email success info."""
    if not events:
        return {}
    sid = getattr(settings, 'sheet_megasheet_id', None)
    if not sid:
        return {event.email_id: (False, 'missing_sheet_id') for event in events}
    try:
        ws_name = getattr(settings, 'expense_ws_title', 'Expenses')
        ws = _open_worksheet_with_retry(sid, ws_name, 'expense')
    except Exception:
        return {event.email_id: (False, 'sheet_open_failed') for event in events}

    snapshot = _fetch_recent_sheet_snapshot(ws, 'expense')
    #If fetch failed (None), we MUST abort to prevent duplicate logging
    if snapshot is None:
        return {event.email_id: (False, 'sheet_read_failed') for event in events}
    existing, existing_row_counts = snapshot

    seen_fp = _load_fingerprints()
    seen_txn = _load_txn_ids()
    seen_msg = _load_message_ids()
    #Union the local index with ids recovered from the sheet itself, so a lost
    #or truncated index.jsonl can no longer resurrect already-logged payments.
    seen_email_ids = set(_load_index().keys())
    seen_email_ids.update(
        r['sheet_email_id'] for r in existing if r.get('sheet_email_id')
    )

    results: Dict[str, Tuple[bool, str]] = {}
    rows: List[List[str]] = []
    idx_events: List[FinanceEvent] = []

    for ev in events:
        # Primary dedup: one email = one payment, never log the same email twice
        if ev.email_id and ev.email_id in seen_email_ids:
            results[ev.email_id] = (False, 'dup_skipped')
            continue

        if ev.txn_id and ev.txn_id in seen_txn:
            try:
                log_action('finance_skip_duplicate', 'expense_txn', f'{ev.counterparty} ${ev.amount:.2f}')
            except Exception:
                pass
            results[ev.email_id] = (False, 'dup_skipped')
            continue
        if ev.message_id and ev.message_id in seen_msg:
            try:
                log_action('finance_skip_duplicate', 'expense_msg', f'{ev.counterparty} ${ev.amount:.2f}')
            except Exception:
                pass
            results[ev.email_id] = (False, 'dup_skipped')
            continue

        fp_hash = _fingerprint_hash_for_event(ev)
        row = _build_expense_row(ev)
        row_key = _sheet_row_key(row)
        if fp_hash in seen_fp or existing_row_counts.get(row_key, 0) > 0 or _looks_duplicate(ev, existing):
            try:
                log_action('finance_skip_duplicate', f'kind=expense', f'{ev.counterparty} ${ev.amount:.2f} on {ev.ts.date().isoformat()}')
            except Exception:
                pass
            #Already in the sheet: settle it so later scans skip it outright.
            _index_settled(ev, 'duplicate')
            results[ev.email_id] = (False, 'dup_skipped')
            continue

        rows.append(row)
        idx_events.append(ev)
        #Optimistically add to seen sets so subsequent items in this batch are checked against this one
        seen_fp.add(fp_hash)
        if ev.email_id:
            seen_email_ids.add(ev.email_id)
        if ev.txn_id:
            seen_txn.add(ev.txn_id)
        if ev.message_id:
            seen_msg.add(ev.message_id)
        existing_row_counts[row_key] = existing_row_counts.get(row_key, 0) + 1
        existing.append(_event_as_sheet_record(ev))

    if rows:
        append_results = await _append_rows_with_retry(ws, rows, 'expense')
        for ev, (ok, msg) in zip(idx_events, append_results):
            results[ev.email_id] = (ok, msg)
            if ok:
                #Already added to seen sets above, but we need to persist to index
                _append_index({
                    "email_id": ev.email_id,
                    "status": ev.direction,
                    "provider": ev.provider,
                    "fingerprint_hash": _fingerprint_hash_for_event(ev),
                    "message_id": ev.message_id,
                    "txn_id": ev.txn_id,
                    "provider_ts": (ev.provider_ts or ev.ts).isoformat(),
                })
            else:
                log_action('finance_sheet_error', 'expense_append_error', msg)

    for ev in events:
        results.setdefault(ev.email_id, (False, 'dup_skipped'))
    return results


async def _process_finance_events(
    bot,
    events: List[FinanceEvent],
    processed: Dict[str, dict],
    notify: bool = True,
) -> List[Tuple[FinanceEvent, Tuple[bool, str], bool]]:
    """Append income/expense events to Sheets, update index, and optionally notify Discord."""
    if not events:
        return []

    buffer_results: List[Tuple[FinanceEvent, Tuple[bool, str], bool]] = []
    unique_events: List[FinanceEvent] = []
    for ev in events:
        if any(_events_maybe_duplicate(ev, other) for other in unique_events):
            try:
                log_action('finance_skip_duplicate', 'buffer', f'{ev.counterparty} ${ev.amount:.2f} on {ev.ts.date().isoformat()}')
            except Exception:
                pass
            buffer_results.append((ev, (False, 'dup_buffer'), False))
            continue
        unique_events.append(ev)

    events = unique_events

    # Fetch sheet snapshot for category inference (nearby logged entries)
    sheet_records: Optional[List[dict]] = None
    has_blank_income = any(
        ev.direction == 'income' and ev.message_blank
        and (not ev.category or ev.category == _DONATION_DEFAULT)
        for ev in events
    )
    if has_blank_income:
        sid = getattr(settings, 'sheet_megasheet_id', None)
        if sid:
            try:
                ws_name = getattr(settings, 'income_ws_title', 'Income')
                ws = sheets_client().open_by_key(sid).worksheet(ws_name)
                sheet_records = _fetch_recent_records(ws, 'income')
            except Exception:
                pass  # Non-critical: inference will still use batch context

    income_events: List[Tuple[FinanceEvent, bool]] = []
    expense_events: List[Tuple[FinanceEvent, bool]] = []

    for event in events:
        if event.direction == 'income':
            _, inferred = _assign_income_category(event, events, sheet_records)
            income_events.append((event, inferred))
        else:
            _, inferred = _assign_expense_category(event)
            expense_events.append((event, inferred))

    income_result_map = await _append_income_rows([ev for ev, _ in income_events])
    expense_result_map = await _append_expense_rows([ev for ev, _ in expense_events])

    results: List[Tuple[FinanceEvent, Tuple[bool, str], bool]] = []

    #Announce only what actually reached the sheet.
    #
    #This used to enumerate the reasons worth silencing, which meant any new
    #failure mode announced itself by default. A transient Sheets 500 on the
    #worksheet open therefore posted a full "Logged Income" notice for every
    #payment in the batch -- for rows that were never written. And because a
    #failed batch is never indexed, the same emails came back on the next run
    #and announced themselves again, without end.
    #
    #Successes are reported per payment. Failures are counted and reported once,
    #because a sheet outage is one event, not one event per payment.
    write_failures: List[str] = []

    def _record(event: FinanceEvent, sheet_res: Tuple[bool, str], inferred: bool) -> bool:
        if sheet_res[0]:
            processed[event.email_id] = True
        else:
            reason = str(sheet_res[1])
            #Deliberate skips are not failures and need no report.
            if not reason.startswith('dup_') and reason not in ('dues_skip',):
                write_failures.append(reason)
        results.append((event, sheet_res, inferred))
        return bool(sheet_res[0])

    for event, inferred in income_events:
        ok = _record(event, income_result_map.get(event.email_id, (False, 'not_logged')), inferred)
        if notify and ok:
            await _notify_logging_channel(
                bot, event, event.direction,
                income_result_map.get(event.email_id, (False, 'not_logged')),
                inferred, event.note.strip() == "")

    for event, inferred in expense_events:
        ok = _record(event, expense_result_map.get(event.email_id, (False, 'not_logged')), inferred)
        if notify and ok:
            await _notify_logging_channel(
                bot, event, event.direction,
                expense_result_map.get(event.email_id, (False, 'not_logged')),
                inferred, event.note.strip() == "")

    if notify and write_failures:
        top = Counter(write_failures).most_common(1)[0][0]
        try:
            log_action('finance_write_failed', f'count={len(write_failures)}', top)
        except Exception:
            pass
        await _notify_channel_text(
            bot,
            f"[Finance] {len(write_failures)} entr"
            f"{'y' if len(write_failures) == 1 else 'ies'} could not be written to "
            f"the sheet ({top}). Nothing was recorded and nothing was lost - they "
            f"will be retried on the next scan."
        )

    results.extend(buffer_results)
    return results


def _collect_recent_finance_events(limit: int) -> Tuple[List[FinanceEvent], int, int]:
    """Return the most recent finance events up to the requested count."""
    if limit <= 0:
        return [], 0, 0
    dues_ids = _load_dues_message_ids()
    resolved_dues_ids = _load_resolved_dues_email_ids()
    if dues_ids:
        _DUES_MESSAGE_IDS.update(dues_ids)
    events: List[FinanceEvent] = []
    scanned = 0
    skipped_dues = 0
    for email in _iter_email_logs_newest_first():
        email_id = email.get('id')
        if not email_id:
            continue
        scanned += 1
        if email_id in dues_ids:
            skipped_dues += 1
            continue
        if email_id in resolved_dues_ids:
            skipped_dues += 1
            continue
        event, status = _classify_email(email)
        if event is None:
            if status == 'dues':
                skipped_dues += 1
            continue
        if event.direction == 'income' and event.email_id in dues_ids:
            skipped_dues += 1
            continue
        events.append(event)
        if len(events) >= limit:
            break
    events.sort(key=lambda ev: ev.ts)
    return events, scanned, skipped_dues


async def _resolve_logging_channel(bot):
    """Return the finance logging channel, or None."""
    ch_id = getattr(settings, 'ch_logging', None) or getattr(settings, 'ch_sandbox', None)
    if not ch_id or not bot:
        return None
    try:
        channel = bot.get_channel(int(ch_id))
    except Exception:
        channel = None
    if not channel:
        try:
            channel = await bot.fetch_channel(int(ch_id))
        except Exception:
            channel = None
    return channel


async def _notify_channel_text(bot, text: str) -> None:
    """Post a single plain message to the finance logging channel."""
    channel = await _resolve_logging_channel(bot)
    if not channel:
        return
    try:
        await safe_send(channel, text)
    except Exception:
        pass


async def _notify_logging_channel(
    bot,
    event: FinanceEvent,
    status: str,
    sheet_result: Tuple[bool, str],
    fallback: bool,
    blank_note: bool,
) -> None:
    ch_id = getattr(settings, 'ch_logging', None) or getattr(settings, 'ch_sandbox', None)
    if not ch_id:
        return
    channel = None
    try:
        channel = bot.get_channel(int(ch_id)) if bot else None
    except Exception:
        channel = None
    if not channel and bot:
        try:
            channel = await bot.fetch_channel(int(ch_id))
        except Exception:
            channel = None
    if not channel:
        return
    direction = 'Income' if event.direction == 'income' else 'Expense'
    amount = f"${event.amount:.2f}"
    note = event.note.strip()
    provider = event.payment_type
    counterparty = event.counterparty or "(unknown)"
    category = event.category or (
        _DONATION_DEFAULT if event.direction == 'income' else _EXPENSE_TYPES['misc']
    )
    sheet_ok, sheet_msg = sheet_result

    extra_details = []
    if fallback:
        extra_details.append("category inferred")
    if not sheet_ok:
        extra_details.append(f"sheet error: {sheet_msg}")

    if blank_note and event.direction == 'income':
        conf = getattr(event, 'category_confidence', '') or ''
        reason = getattr(event, 'category_reason', '') or ''
        if conf == "low":
            type_reason = (f"NEEDS REVIEW - best guess {category} ({reason}). "
                           f"Correct it on the sheet if that's wrong.")
        elif fallback:
            type_reason = f"Filed as {category} ({reason})."
        else:
            type_reason = f"Filed as {category}."
        body = (
            f"{provider}: {amount} from {counterparty}, message: {MESSAGE_EMPTY_SENTINEL}.\n"
            f"{type_reason}"
        )
        if extra_details:
            body += f" ({'; '.join(extra_details)})"
        body = f"[Finance] Logged {direction} → {category}\n" + body
    else:
        note_display = note or MESSAGE_EMPTY_SENTINEL
        body_lines = [
            f"[Finance] Logged {direction} → {category}",
            f"Amount: {amount} via {provider}",
            f"Counterparty: {counterparty}",
            f"Note: {note_display}",
        ]
        if extra_details:
            body_lines.append(f"Details: {', '.join(extra_details)}")
        body = "\n".join(body_lines)
    try:
        await safe_send(channel, body)
    except Exception:
        pass


_CATEGORY_WINDOW_HOURS = 5

#Rails people use standing in front of you at a table. A bake sale runs on these;
#an online donation drive runs on PayPal/GoFundMe.
_INPERSON_RAILS = {"venmo", "cashapp", "cash app", "cash", "zelle"}
_ONLINE_RAILS = {"paypal", "gofundme"}

#Score at or beyond this magnitude is acted on; anything inside the band is
#flagged for an officer instead of being guessed silently.
_CATEGORY_CONFIDENCE_CUTOFF = 2.0


def _day_context(
    event: FinanceEvent,
    batch_events: List[FinanceEvent],
    sheet_records: Optional[List[dict]],
) -> Tuple[int, float, bool]:
    """Summarise the day an income event landed on.

    Returns (payments_that_day, in_person_rail_fraction, payer_paid_before_today).
    Volume alone is misleading: a 61-payment day of PayPal donations looks
    exactly like a 61-payment bake sale until you check which rails were used.
    """
    ev_date = (event.provider_ts or event.ts).date()
    rails: List[str] = []
    repeat = False
    payer = _norm_text(event.counterparty)

    for other in batch_events or []:
        if other is event or other.direction != 'income':
            continue
        if (other.provider_ts or other.ts).date() != ev_date:
            continue
        rails.append((other.payment_type or "").strip().lower())
        if payer and _norm_text(other.counterparty) == payer:
            repeat = True

    for rec in sheet_records or []:
        if rec.get('date') != ev_date:
            continue
        rails.append((rec.get('provider') or "").strip().lower())
        if payer and _norm_text(rec.get('counterparty') or "") == payer:
            repeat = True

    rails.append((event.payment_type or "").strip().lower())
    known = [r for r in rails if r in _INPERSON_RAILS or r in _ONLINE_RAILS]
    frac = (sum(1 for r in known if r in _INPERSON_RAILS) / len(known)) if known else 0.0
    return len(rails), frac, repeat


def _score_fundraiser_signal(
    event: FinanceEvent,
    batch_events: List[FinanceEvent],
    sheet_records: Optional[List[dict]],
) -> Tuple[float, List[str]]:
    """Score an income event: positive means fundraiser, negative means donation.

    Weights come from the reviewed history in the megasheet. Against rows an
    officer categorised by hand this separates the two classes with ~96%
    accuracy, versus 47% for the old "default everything to Donations".
    """
    s = 0.0
    why: List[str] = []
    note = (event.note or "").lower()
    amt = float(event.amount)

    if any(w in note for w in DONATION_KEYWORDS):
        s -= 3.0
        why.append("donation wording")

    #Amount shape. A cookie costs whole dollars; a donation that arrives with
    #cents has had a processing fee taken off the top (PayPal's ~3% leaves
    #multiples of 0.97, e.g. $4.00 -> $3.88). Only 1.6% of fundraiser sales in
    #the history carry cents, against 22.9% of donations.
    if round((amt * 100) % 100) != 0:
        s -= 2.0
        why.append("non-round amount")
    else:
        s += 0.5
    if abs(amt - float(getattr(settings, 'dues_base_amount', 15.0) or 15.0)) < 0.01:
        s -= 1.5
        why.append("matches membership amount")
    if amt < 5:
        s += 1.0
        why.append("under $5")
    elif amt < 10:
        s += 0.5
    elif amt >= 20:
        #Bake-sale items are cheap: the fundraiser median is $4 and 79% of sales
        #are under $10. A $20+ payment is donation-sized even at a table.
        s -= 1.0
        why.append("$20+")
    if amt >= 50:
        s -= 1.0
        why.append("$50+")
    if amt >= 10 and amt % 25 == 0:
        s -= 1.0
        why.append("multiple of $25")

    count, frac, repeat = _day_context(event, batch_events, sheet_records)
    #A busy in-person day only implies a fundraiser for fundraiser-sized amounts.
    #2024-08-09 was 27 Venmo payments of $10-$100: a donation drive collected at
    #a table, not a bake sale. Volume plus rail cannot outvote the amount.
    if count >= 8 and frac >= 0.6 and amt <= 15:
        s += 2.0
        why.append(f"{count} payments that day, {frac:.0%} in-person rails")
    elif count >= 8 and frac >= 0.6:
        s += 0.5
        why.append(f"busy in-person day but ${amt:.0f} is large for a sale")
    elif count >= 8:
        s -= 0.5
        why.append(f"{count} payments that day but mostly online rails")
    elif count <= 1:
        s -= 1.0
        why.append("only payment that day")
    if repeat:
        s += 0.5
        why.append("same payer already paid today")

    if (event.payment_type or "").strip().lower() in _ONLINE_RAILS:
        s -= 1.0
        why.append(event.payment_type)

    return s, why


def _assign_income_category(
    event: FinanceEvent,
    batch_events: List[FinanceEvent],
    sheet_records: Optional[List[dict]] = None,
) -> Tuple[str, bool]:
    """Lock in category, borrowing context from nearby events within a time window.

    If the event has a concrete keyword-based category (not the default
    "Donations"), keep it.  Otherwise, look at income events within a
    configurable time window (default 5 hours) — both from the current batch
    and from previously logged sheet entries — and infer the dominant
    non-donation category if at least 2 nearby entries share it.
    """
    from datetime import timedelta

    # Keywords stay in front of everything: a note that names a cookie or a
    # sticker is better evidence than any amount or timing heuristic.
    if event.category and event.category != _DONATION_DEFAULT:
        event.category_confidence = "keyword"
        event.category_reason = "matched a category keyword"
        return event.category, False

    # An explicit donation word is equally decisive in the other direction.
    if any(w in (event.note or "").lower() for w in DONATION_KEYWORDS):
        event.category = _DONATION_DEFAULT
        event.category_confidence = "keyword"
        event.category_reason = "donation wording in the note"
        return event.category, False

    # Collect categories from nearby events within the time window
    window = timedelta(hours=_CATEGORY_WINDOW_HOURS)
    ev_ts = event.provider_ts or event.ts
    nearby_cats: Counter = Counter()

    # Count from current batch (already-categorised events processed before this one)
    for other in batch_events:
        if other is event:
            continue
        if other.direction != 'income':
            continue
        other_cat = other.category
        if not other_cat or other_cat == _DONATION_DEFAULT:
            continue
        other_ts = other.provider_ts or other.ts
        if abs((ev_ts - other_ts).total_seconds()) <= window.total_seconds():
            nearby_cats[other_cat] += 1

    # Count from sheet snapshot (previously logged entries)
    if sheet_records:
        ev_date = ev_ts.date()
        for rec in sheet_records:
            r_date = rec.get('date')
            if not r_date:
                continue
            # Quick filter: only look at entries within 1 day (dates don't have times)
            if abs((r_date - ev_date).days) > 1:
                continue
            r_cat = (rec.get('income_type') or '').strip()
            if not r_cat or r_cat == _DONATION_DEFAULT:
                continue
            nearby_cats[r_cat] += 1

    # Nearby entries beat any circumstantial signal: they are categories a
    # keyword hit or an officer already assigned to payments from the same few
    # hours, so this is label propagation rather than guesswork. Measured at
    # 93% against officer-labelled history, well ahead of the scorer below.
    if nearby_cats:
        top, num = nearby_cats.most_common(1)[0]
        if num >= 2:
            event.category = top
            event.category_confidence = "high"
            event.category_reason = f"{num} nearby entries filed as {top}"
            return top, True

    # Nothing nearby to borrow from, so weigh the circumstantial signals:
    # amount shape, what kind of day it was, and which rail carried it.
    score, why = _score_fundraiser_signal(event, batch_events, sheet_records)
    event.category_reason = ", ".join(why) if why else "no distinguishing signal"

    if score > 0:
        # Foods/Goods is the fundraiser fallback because it outnumbers Other
        # Fundraisers roughly 10:1 in the history.
        event.category = _INCOME_TYPES["foods_goods"]
    else:
        event.category = _DONATION_DEFAULT

    # Inside the confidence band the answer is a guess. Say so rather than
    # presenting a silent default as fact -- an uncategorised row an officer
    # fixes beats a miscategorised one nobody notices.
    event.category_confidence = "high" if abs(score) >= _CATEGORY_CONFIDENCE_CUTOFF else "low"
    if event.category_confidence == "low":
        try:
            log_action('finance_category_uncertain',
                       f'{event.counterparty} ${event.amount:.2f}',
                       f'chose={event.category} score={score:+.1f} ({event.category_reason})')
        except Exception:
            pass
    return event.category, True


def _assign_expense_category(event: FinanceEvent) -> Tuple[str, bool]:
    """Default expense classification when heuristics came up empty."""
    if event.category:
        return event.category, False
    event.category = _EXPENSE_TYPES['misc']
    return event.category, True


def _classify_email(email: dict) -> Tuple[Optional[FinanceEvent], str]:
    """Route an email through provider-specific classifiers."""
    provider = detect_provider(
        email.get("from", ""),
        email.get("subject", ""),
        email.get("content", ""),
    )
    if not provider:
        return None, "unsupported"
    if provider == 'venmo':
        return _classify_venmo(email)
    if provider == 'cashapp':
        return _classify_cashapp(email)
    if provider == 'paypal':
        return _classify_paypal(email)
    return None, "unsupported"


async def _check_dues_corroboration(counterparty: str, provider: str, bot) -> bool:
    """Check if there's corroborating evidence for a dues payment.
    
    Returns True if:
    - A matching entry exists in the dues portal channel (recent messages)
    - OR a matching membership application exists (unverified, current semester)
    """
    from . import dues as dues_module
    from rapidfuzz import fuzz
    
    name_norm = (counterparty or "").strip().lower()
    if not name_norm:
        return False
    
    #Check membership application list for unverified entry matching this name
    try:
        rows = dues_module._load_membership_rows()
        cur_sem = dues_module._current_semester_label()
        cur_sem_norm = dues_module._norm_sem_label(cur_sem).lower()
        
        for row in rows:
            #Only check current semester entries
            row_sem = dues_module._norm_sem_label(row.get("semester", "")).lower()
            if row_sem != cur_sem_norm:
                continue
            
            #Match by name (fuzzy)
            row_name = (row.get("full_name") or "").strip().lower()
            if row_name and fuzz.ratio(name_norm, row_name) >= 80:
                log_action("pending_dues_corroborate", f"name={counterparty}", "membership_match")
                return True
            
            #Match by payment username if provided
            pay_user = (row.get("payment_username") or "").strip().lower()
            if pay_user and (pay_user in name_norm or name_norm in pay_user):
                log_action("pending_dues_corroborate", f"name={counterparty}", "payment_user_match")
                return True
    except Exception as e:
        log_action("pending_dues_corroborate_error", "membership", str(e))
    
    #Check dues portal channel for recent messages matching this name
    try:
        portal_ch_id = int(getattr(settings, "ch_due_portal", 0) or 0)
        if portal_ch_id and bot:
            ch = bot.get_channel(portal_ch_id)
            if ch:
                async for msg in ch.history(limit=200):
                    content = (msg.content or "").strip().lower()
                    #Check if counterparty name appears in message
                    if name_norm in content or fuzz.partial_ratio(name_norm, content) >= 85:
                        #Also verify provider if mentioned
                        prov_norm = (provider or "").lower()
                        if prov_norm in content or not prov_norm:
                            log_action("pending_dues_corroborate", f"name={counterparty}", "portal_match")
                            return True
    except Exception as e:
        log_action("pending_dues_corroborate_error", "portal", str(e))
    
    return False


async def _process_pending_dues(bot) -> None:
    """Process pending dues: check for corroboration or expiry.
    
    For each pending payment:
    - If corroborated (portal message or membership app) → remove (dues pipeline handles)
    - If expired (3+ days) → log as income donations, remove from pending
    """
    from datetime import timedelta
    
    records = _load_pending_dues()
    if not records:
        return
    
    now = datetime.now(timezone.utc)
    processed = _load_index()
    income_events: List[FinanceEvent] = []
    to_remove: List[str] = []
    
    for rec in records:
        email_id = rec.get("email_id")
        if not email_id:
            continue
        
        counterparty = rec.get("counterparty", "")
        provider = rec.get("provider", "")
        expires_str = rec.get("expires", "")
        
        #Parse expiry
        try:
            expires = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        except Exception:
            expires = now + timedelta(days=1)  #Default to keeping if parse fails
        
        #Check if corroborated (found in dues portal or membership sheet)
        corroborated = await _check_dues_corroboration(counterparty, provider, bot)
        
        if corroborated:
            _append_resolved_dues_email_id(email_id, "corroborated_as_dues")
            to_remove.append(email_id)
            log_action("pending_dues_resolved", f"counterparty={counterparty}", "corroborated_as_dues")
            continue
        
        #Check if expired
        if now >= expires:
            #Expired without corroboration → log as income
            try:
                ts = datetime.fromisoformat(rec.get("ts", "").replace("Z", "+00:00"))
            except Exception:
                ts = now
            
            event = FinanceEvent(
                email_id=email_id,
                provider=provider,
                counterparty=counterparty,
                note=rec.get("note", ""),
                amount=float(rec.get("amount", 0.0)),
                direction="income",
                category=_DONATION_DEFAULT,
                ts=ts,
                raw_subject=rec.get("raw_subject", ""),
                raw_content=rec.get("raw_content", ""),
                message_blank=not bool(rec.get("note", "").strip()),
            )
            income_events.append(event)
            _append_resolved_dues_email_id(email_id, "expired_to_income")
            to_remove.append(email_id)
            log_action("pending_dues_resolved", f"counterparty={counterparty}", "expired_to_income")
    
    #Log expired payments as income
    if income_events:
        await _process_finance_events(bot, income_events, processed, notify=True)
    
    #Remove processed records
    if to_remove:
        remaining = [r for r in records if r.get("email_id") not in to_remove]
        _save_pending_dues(remaining)


async def process_financial_emails(bot) -> None:
    """Read new finance emails, write sheet rows, and notify Discord."""
    async with FINANCE_LOCK:
        processed = _load_index()
        processed_ids = set(processed.keys())
        dues_ids = _load_dues_message_ids()
        resolved_dues_ids = _load_resolved_dues_email_ids()
        pending_ids = _get_pending_due_ids()
        events: List[FinanceEvent] = []
        seen_this_batch: set[str] = set()  #Prevent same-batch duplicates
        for email in _iter_email_logs():
            email_id = email.get('id')
            if not email_id:
                continue
            if email_id in processed_ids:
                continue
            if email_id in dues_ids:
                continue
            if email_id in resolved_dues_ids:
                continue
            if email_id in pending_ids:
                continue
            if email_id in seen_this_batch:
                continue
            seen_this_batch.add(email_id)
            event, status = _classify_email(email)
            if event is None:
                continue
            
            #$15 income payments go to pending for corroboration check
            if (event.direction == "income" and 
                _is_potential_dues_amount(event.amount) and 
                not _is_likely_dues(event.amount, f"{event.raw_subject} {event.note}")):
                _add_pending_due(event)
                continue
            
            events.append(event)
        
        #Process immediate events
        if events:
            await _process_finance_events(bot, events, processed, notify=True)
        
        #Check pending dues for corroboration or expiry
        await _process_pending_dues(bot)




async def handle_log_recent_finances(intent, ctx) -> None:
    """Manual command: attempt to log the last N finance events from email logs."""
    ch = ctx.get("channel")
    bot = ctx.get("bot")
    if ch is None:
        return

    msg = ctx.get("message")
    bot_user = getattr(bot, 'user', None) if bot else None
    thumb_added = False

    async def _flip_reaction(success: bool) -> None:
        if not msg:
            return
        emoji = "✅" if success else "❌"
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass
        if thumb_added and bot_user:
            try:
                await msg.remove_reaction("👍", bot_user)
            except Exception:
                pass

    try:
        try:
            if msg:
                await msg.add_reaction("👍")
                thumb_added = True
        except Exception:
            pass

        raw_count = (intent.data or {}).get("count") if intent else None
        try:
            count = int(raw_count) if raw_count is not None else 10
        except Exception:
            count = 10
        count = max(1, min(count, 200))

        log_action('finance_manual_log_begin', f'count={count}', f'ch={getattr(ch, "id", None)}')

        async with FINANCE_LOCK:
            processed = _load_index()
            initial_processed = set(processed.keys())
            events, scanned, skipped_dues = _collect_recent_finance_events(count)

            if not events:
                note = f"Scanned {scanned} email(s)"
                if skipped_dues:
                    note += f"; skipped {skipped_dues} dues entries"
                await safe_send(ch, f"No finance entries found. {note}.")
                log_action('finance_manual_log_done', f'count={count}', 'found=0')
                await _flip_reaction(True)
                return

            to_process = [ev for ev in events if ev.email_id not in initial_processed]
            result_map: Dict[str, Tuple[Tuple[bool, str], bool]] = {}
            if to_process:
                processed_results = await _process_finance_events(bot, to_process, processed, notify=True)
                for ev, sheet_res, inferred in processed_results:
                    result_map[ev.email_id] = (sheet_res, inferred)

        def _shorten(text: str, limit: int = 60) -> str:
            clean = (text or "").replace("\n", " ").strip()
            if len(clean) <= limit:
                return clean
            return clean[: limit - 1] + "…"

        summary_lines: List[str] = []
        logged = 0
        duplicates = 0
        already = 0
        failed = 0

        header = f"Finance log results — requested {count}, processed {len(events)}"
        header += f", scanned {scanned} email(s)"
        if skipped_dues:
            header += f", skipped {skipped_dues} dues"
        summary_lines.append(header)

        for idx, event in enumerate(events, start=1):
            base = f"{idx}. {'Income' if event.direction == 'income' else 'Expense'}"
            base += f" • {event.payment_type} • ${event.amount:.2f} • {event.counterparty or '(unknown)'}"
            category = event.category or (
                _DONATION_DEFAULT if event.direction == 'income' else _EXPENSE_TYPES['misc']
            )
            note_text = event.note.strip() or MESSAGE_EMPTY_SENTINEL
            sheet_info = result_map.get(event.email_id)
            if event.email_id in initial_processed and event.email_id not in result_map:
                status_text = "already logged"
                already += 1
            elif sheet_info:
                (ok, msg), inferred = sheet_info
                if ok:
                    logged += 1
                    detail = "logged"
                    if msg and msg != 'ok':
                        detail += f" ({msg})"
                    if inferred:
                        detail += "; category inferred"
                    status_text = detail
                else:
                    if str(msg).startswith('dup_'):
                        duplicates += 1
                        status_text = "duplicate (skipped)"
                    else:
                        failed += 1
                        status_text = f"failed ({msg})"
            else:
                #Should not happen, but guard anyway
                status_text = "no action"

            line = f"{base} — {status_text} ({category})"
            short_note = _shorten(note_text)
            if short_note and short_note != MESSAGE_EMPTY_SENTINEL:
                line += f" • note: {short_note}"
            elif event.direction == 'income':
                line += f" • note: {MESSAGE_EMPTY_SENTINEL}"
            summary_lines.append(line)

        footer_bits = []
        if logged:
            footer_bits.append(f"logged {logged}")
        if duplicates:
            footer_bits.append(f"duplicates {duplicates}")
        if already:
            footer_bits.append(f"already logged {already}")
        if failed:
            footer_bits.append(f"failed {failed}")
        if footer_bits:
            summary_lines.append("Totals: " + ", ".join(footer_bits))

        #Send in chunks to respect Discord limits
        chunk: List[str] = []
        char_count = 0
        for line in summary_lines:
            line_len = len(line) + 1
            if char_count + line_len > 1900 and chunk:
                await safe_send(ch, "\n".join(chunk))
                chunk = []
                char_count = 0
            chunk.append(line)
            char_count += line_len
        if chunk:
            await safe_send(ch, "\n".join(chunk))

        log_action(
            'finance_manual_log_done',
            f'count={count}',
            f'found={len(events)} logged={logged} dup={duplicates} already={already} failed={failed}',
        )

        await _flip_reaction(True)

    except Exception as e:
        await safe_send(ch, f"Finance logging error: {e}")
        log_action('finance_manual_log_error', '', str(e))
        await _flip_reaction(False)
    else:
        if not msg and thumb_added and bot_user:
            pass
