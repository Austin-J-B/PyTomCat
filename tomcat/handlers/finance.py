"""Non-dues finance ingestion: classify emails, build Sheets payloads, notify sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter, defaultdict
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

FOODS_GOODS_KEYWORDS = {
    "bake", "cookie", "cookies", "brownie", "brownies", "cupcake", "cupcakes",
    "drink", "drinks", "soda", "snack", "snacks", "lemonade", "scone", "dessert",
    "food", "goods", "pastry", "pastries", "chai", "coffee", "tea", "candy",
}
OTHER_FUNDRAISER_KEYWORDS = {
    "sticker", "stickers", "merch", "shirt", "shirts", "hoodie", "hoodies",
    "sweater", "pin", "pins", "button", "buttons", "keychain", "keychains",
    "crochet", "plush", "plushie", "bookmark", "bracelet", "earring", "earrings",
}
ADOPTION_KEYWORDS = {
    "adoption", "adopt", "adopting", "adoption fee", "adopt fee", "adopted",
}
DEDUCTION_WORDS = {"dues", "membership", "member", "due"}

VET_KEYWORDS = {
    "vet", "veterinary", "clinic", "vaccine", "vaccination", "spay", "neuter",
    "appointment", "banfield", "animal hospital", "exam", "surgery", "meds", "medicine",
}
FOOD_EXPENSE_KEYWORDS = {
    "petco", "petsmart", "pet smart", "chewy", "cat food", "food", "litter", "kibble",
    "treat", "treats", "wet food", "dry food", "purina", "friskies", "royal canin",
}
STORAGE_KEYWORDS = {
    "py store", "storage unit", "storage fee", "ps store here", "ps store",
}
WEBSITE_KEYWORDS = {
    "wix", "domain", "website", "site fee", "hosting", "catsofuta.org", "squarespace",
}
SUPPLIES_KEYWORDS = {
    "supply", "supplies", "poster", "flyer", "table", "banner", "balloon", "cups",
    "plates", "sign", "marker", "ink", "toner", "printing", "tape", "scissors",
    "decor", "decoration", "craft", "glue", "string", "paint", "brush", "bag",
}

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
    direction: str  # "income" or "expense"
    category: Optional[str]
    ts: datetime
    raw_subject: str
    raw_content: str
    message_blank: bool

    @property
    def payment_type(self) -> str:
        """Pretty provider label for Sheets output."""
        return PROVIDER_NAMES.get(self.provider, self.provider.title())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_index() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not os.path.exists(FINANCE_INDEX):
        return out
    try:
        with open(FINANCE_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    eid = obj.get("email_id")
                    if eid:
                        out[eid] = obj
                except Exception:
                    continue
    except Exception:
        return out
    return out


def _append_index(record: dict) -> None:
    record.setdefault("ts", _now_iso())
    with open(FINANCE_INDEX, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_fingerprints() -> Set[str]:
    """Return set of previously logged transaction fingerprints."""
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
                fp = obj.get("fingerprint")
                if isinstance(fp, str) and fp:
                    fps.add(fp)
    except Exception:
        return fps
    return fps


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


def _clean_counterparty(name: str) -> str:
    """Normalize sender/payee strings pulled from email subjects."""
    return name.strip().strip('.')


def _is_likely_dues(amount: Optional[float], text: str) -> bool:
    """Heuristic guardrail so finance only logs true non-dues entries."""
    text_low = text.lower()
    if any(word in text_low for word in DEDUCTION_WORDS):
        return True
    if amount is None:
        return False
    return abs(amount - _DUES_AMOUNT) <= _DUES_TOL


def _extract_note(text: str) -> str:
    """Trim boilerplate prefixes and whitespace from payment notes."""
    if not text:
        return ""
    text = text.strip()
    if text.startswith("for "):
        text = text[4:]
    return text.strip()


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def _fingerprint(ev: "FinanceEvent") -> str:
    d = ev.ts.date().isoformat()
    base = f"{ev.direction}|{d}|{ev.amount:.2f}|{_norm_text(ev.counterparty)}|{_norm_text(ev.note)[:40]}|{ev.provider}"
    return base


# --- Venmo-specific parsing of payment notifications ---

def _classify_venmo(email: dict) -> Tuple[Optional[FinanceEvent], str]:
    subject = email.get("subject", "")
    content = email.get("content", "")
    text = subject.strip()
    body = content
    ts = _parse_timestamp(email)

    # Paid you
    m = re.match(r"^(?P<name>.+?)\s+(?:paid|sent)\s+you\s+\$?(?P<amount>[0-9.,]+)(?:\s+for\s+(?P<note>.+))?", text, re.I)
    if m:
        name = _clean_counterparty(m.group("name"))
        amount = float(m.group("amount").replace(",", ""))
        note = _extract_note(m.group("note") or _extract_note(_find_note_in_body(body)))
        blank = not bool(note.strip())
        if _is_likely_dues(amount, f"{subject} {note}"):
            return (None, "dues")
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="venmo",
            counterparty=name,
            note=note,
            amount=amount,
            direction="income",
            category=_categorize_income(note or subject),
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=blank,
        ), "income")

    # You paid someone
    m = re.match(r"^you\s+paid\s+(?P<name>.+?)\s+\$?(?P<amount>[0-9.,]+)(?:\s+for\s+(?P<note>.+))?", text, re.I)
    if m:
        name = _clean_counterparty(m.group("name"))
        amount = float(m.group("amount").replace(",", ""))
        note = _extract_note(m.group("note") or _extract_note(_find_note_in_body(body)))
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
        ), "expense")

    # Receipt from
    m = re.match(r"^receipt\s+from\s+(?P<name>.+?)-?\s*\$?(?P<amount>[0-9.,]+)", text, re.I)
    if m:
        name = _clean_counterparty(m.group("name"))
        amount = float(m.group("amount").replace(",", ""))
        note = _extract_note(_find_note_in_body(body))
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
        ), "expense")

    return (None, "ignore")



# --- Cash App parsing logic mirrors Venmo but handles spending receipts ---

def _classify_cashapp(email: dict) -> Tuple[Optional[FinanceEvent], str]:
    subject = email.get("subject", "")
    content = email.get("content", "")
    text = subject.strip()
    body = content
    ts = _parse_timestamp(email)

    m = re.match(r"^(?P<name>.+?)\s+sent\s+you\s+\$?(?P<amount>[0-9.,]+)(?:\s+for\s+(?P<note>.+))?", text, re.I)
    if m:
        name = _clean_counterparty(m.group("name"))
        amount = float(m.group("amount").replace(",", ""))
        note = _extract_note(m.group("note") or _extract_note(_find_note_in_body(body)))
        blank = not bool(note.strip())
        if _is_likely_dues(amount, f"{subject} {note}"):
            return (None, "dues")
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="cashapp",
            counterparty=name,
            note=note,
            amount=amount,
            direction="income",
            category=_categorize_income(note or subject),
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=blank,
        ), "income")

    # Spending via Cash App
    m = re.search(r"You spent \$([0-9.,]+)\s+at\s+([^\n]+)", content, re.I)
    if m:
        amount = float(m.group(1).replace(",", ""))
        name = _clean_counterparty(m.group(2))
        note = ""
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
            message_blank=True,
        ), "expense")

    return (None, "ignore")



# --- PayPal notifications: more varied subjects/receipts ---

def _classify_paypal(email: dict) -> Tuple[Optional[FinanceEvent], str]:
    subject = email.get("subject", "")
    content = email.get("content", "")
    text = subject.strip()
    ts = _parse_timestamp(email)

    if "you've got money" in text.lower() or "money received" in text.lower():
        m = re.search(r"([A-Za-z][A-Za-z '\.-]+)\s+sent\s+you\s+\$([0-9.,]+)", content)
        if not m:
            m = re.search(r"Money received\s+from\s+([^\n]+)\s+\$([0-9.,]+)", content)
        if m:
            name = _clean_counterparty(m.group(1))
            amount = float(m.group(2).replace(",", ""))
            note = _extract_note(_find_note_in_body(content))
            blank = not bool(note.strip())
            if _is_likely_dues(amount, f"{subject} {note}"):
                return (None, "dues")
            return (FinanceEvent(
                email_id=email.get("id", ""),
                provider="paypal",
                counterparty=name,
                note=note,
                amount=amount,
                direction="income",
                category=_categorize_income(note or subject),
                ts=ts,
                raw_subject=subject,
                raw_content=content,
                message_blank=blank,
            ), "income")

    # Payment sent (expense)
    m = re.search(r"You\s+sent\s+a?\s*\$([0-9.,]+)\s*(?:usd)?\s+payment\s+to\s+([^\n]+)", content, re.I)
    if m:
        amount = float(m.group(1).replace(",", ""))
        name = _clean_counterparty(m.group(2))
        note = _extract_note(_find_note_in_body(content))
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
        ), "expense")

    # Statements or receipts that imply expenses can be ignored during ingestion
    if re.search(r"statement", text, re.I):
        return (None, "ignore")

    # Payouts like "Bonfire: $36.77 USD" treated as income by default
    m = re.match(r"^(?P<name>.+?):\s*\$?(?P<amount>[0-9.,]+)\s*(?:usd)?", text, re.I)
    if m:
        name = _clean_counterparty(m.group("name"))
        amount = float(m.group("amount").replace(",", ""))
        note = _extract_note(_find_note_in_body(content))
        blank = not bool(note.strip())
        if _is_likely_dues(amount, f"{subject} {note}"):
            return (None, "dues")
        return (FinanceEvent(
            email_id=email.get("id", ""),
            provider="paypal",
            counterparty=name,
            note=note,
            amount=amount,
            direction="income",
            category=_categorize_income(note or subject),
            ts=ts,
            raw_subject=subject,
            raw_content=content,
            message_blank=blank,
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


def _categorize_income(text: str) -> Optional[str]:
    """Map free-form notes to our Income form categories."""
    lower = (text or "").lower()
    if any(word in lower for word in ADOPTION_KEYWORDS):
        return _INCOME_TYPES["adoption"]
    if any(word in lower for word in FOODS_GOODS_KEYWORDS):
        return _INCOME_TYPES["foods_goods"]
    if any(word in lower for word in OTHER_FUNDRAISER_KEYWORDS):
        return _INCOME_TYPES["other"]
    if lower:
        return None
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
    ts = event.ts.astimezone(timezone.utc)
    timestamp = format_mmddyyyy(ts)
    month = ts.strftime('%B')
    year = str(ts.year)
    name_field = f"{event.counterparty}"
    note = event.note.strip()
    if note:
        name_field += f" (Message: {note})"
    else:
        name_field += " (Message: none)"
    name_field += " [Recorded by the TomCat bot]"
    return [
        timestamp,
        month,
        year,
        "",
        name_field,
        event.category or _DONATION_DEFAULT,
        f"{event.amount:.2f}",
        event.payment_type,
    ]


def _build_expense_row(event: FinanceEvent) -> List[str]:
    """Translate a FinanceEvent into the Expenses sheet row schema."""
    ts = event.ts.astimezone(timezone.utc)
    timestamp = format_mmddyyyy(ts)
    month = ts.strftime('%B')
    year = str(ts.year)
    name_field = f"{event.counterparty}"
    note = event.note.strip()
    if note:
        name_field += f" (Message: {note})"
    name_field += " [Recorded by the TomCat bot]"
    return [
        timestamp,
        month,
        year,
        name_field,
        event.category or _EXPENSE_TYPES['misc'],
        f"{event.amount:.2f}",
    ]


async def _append_rows_with_retry(ws, rows: List[List[str]], label: str) -> List[Tuple[bool, str]]:
    """Batch append rows while automatically backing off on quota errors."""
    if not rows:
        return []
    if hasattr(ws, "append_rows"):
        try:
            ws.append_rows(rows, value_input_option='USER_ENTERED')
            return [(True, "ok") for _ in rows]
        except Exception as e:
            log_action('finance_sheet_error', f'{label}_append_batch', str(e))
    results: List[Tuple[bool, str]] = []
    for row in rows:
        delay = 1.0
        success = False
        last_error = "ok"
        for attempt in range(5):
            try:
                ws.append_row(row, value_input_option='USER_ENTERED')
                success = True
                last_error = "ok"
                break
            except Exception as e:
                last_error = str(e)
                msg_lower = last_error.lower()
                if 'quota' in msg_lower or '429' in msg_lower:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, 8.0)
                    continue
                break
        if not success:
            log_action('finance_sheet_error', f'{label}_append_row', last_error)
        results.append((success, last_error))
    return results


def _fetch_recent_records(ws, kind: str, max_rows: int = 800) -> List[dict]:
    """Fetch recent rows from a worksheet and normalize into comparable records.
    kind: 'income' or 'expense'
    """
    try:
        vals = ws.get_all_values()
    except Exception as e:
        log_action('finance_sheet_error', f'{kind}_fetch', str(e))
        return []
    if not vals:
        return []
    rows = vals[-max_rows:] if len(vals) > max_rows else vals
    recs: List[dict] = []
    for r in rows:
        try:
            # Normalize columns by schema
            if kind == 'income':
                # [date, month, year, '', name_field, category, amount, payment_type]
                date_s = (r[0] if len(r) > 0 else '').strip()
                name_field = (r[4] if len(r) > 4 else '').strip()
                amount_s = (r[6] if len(r) > 6 else '').strip().replace('$', '')
            else:
                # [date, month, year, name_field, category, amount]
                date_s = (r[0] if len(r) > 0 else '').strip()
                name_field = (r[3] if len(r) > 3 else '').strip()
                amount_s = (r[5] if len(r) > 5 else '').strip().replace('$', '')
            # Parse date
            dt = None
            if date_s:
                try:
                    # mm/dd/yyyy
                    m, d, y = [int(x) for x in date_s.split('/')]
                    dt = datetime(y, m, d).date()
                except Exception:
                    dt = None
            # Parse amount
            try:
                amt = float(amount_s.replace(',', '')) if amount_s else None
            except Exception:
                amt = None
            recs.append({
                'date': dt,
                'amount': amt,
                'text': name_field,
            })
        except Exception:
            continue
    return recs


def _similar_enough(a: str, b: str) -> bool:
    a = _norm_text(a)
    b = _norm_text(b)
    if not a or not b:
        return False
    try:
        from rapidfuzz import fuzz as rf_fuzz  # type: ignore
        score = rf_fuzz.token_set_ratio(a, b)
        return score >= 82
    except Exception:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _looks_duplicate(ev: "FinanceEvent", recs: List[dict]) -> bool:
    """Heuristic: same day, amount within $1, and similar counterparty/note text."""
    ev_date = ev.ts.date()
    ev_amt = ev.amount
    needle = f"{ev.counterparty} {ev.note}".strip()
    for r in recs:
        if r.get('date') != ev_date:
            continue
        amt = r.get('amount')
        if amt is None:
            continue
        if abs(float(amt) - float(ev_amt)) > 1.00:
            continue
        text = r.get('text') or ''
        if _similar_enough(text, needle) or _similar_enough(text, ev.counterparty):
            return True
    return False


async def _append_income_rows(events: List[FinanceEvent]) -> Dict[str, Tuple[bool, str]]:
    """Flush income events to Sheets and return per-email success info."""
    if not events:
        return {}
    sid = getattr(settings, 'sheet_megasheet_id', None)
    if not sid:
        return {event.email_id: (False, 'missing_sheet_id') for event in events}
    try:
        ws_name = getattr(settings, 'income_ws_title', 'Income')
        ws = sheets_client().open_by_key(sid).worksheet(ws_name)
    except Exception as e:
        log_action('finance_sheet_error', 'income_open', str(e))
        return {event.email_id: (False, str(e)) for event in events}

    # Load dedupe context
    existing = _fetch_recent_records(ws, 'income')
    seen_fp = _load_fingerprints()
    results: Dict[str, Tuple[bool, str]] = {}
    rows: List[List[str]] = []
    idx_map: List[str] = []  # map row index -> email_id
    for ev in events:
        fp = _fingerprint(ev)
        if fp in seen_fp or _looks_duplicate(ev, existing):
            try:
                log_action('finance_skip_duplicate', f'kind=income', f'{ev.counterparty} ${ev.amount:.2f} on {ev.ts.date().isoformat()}')
            except Exception:
                pass
            results[ev.email_id] = (False, 'dup_skipped')
            continue
        rows.append(_build_income_row(ev))
        idx_map.append(ev.email_id)
    if rows:
        appended = await _append_rows_with_retry(ws, rows, 'income')
        for i, eid in enumerate(idx_map):
            results[eid] = appended[i]
    # Ensure all events present in mapping
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
        ws = sheets_client().open_by_key(sid).worksheet(ws_name)
    except Exception as e:
        log_action('finance_sheet_error', 'expense_open', str(e))
        return {event.email_id: (False, str(e)) for event in events}

    existing = _fetch_recent_records(ws, 'expense')
    seen_fp = _load_fingerprints()
    results: Dict[str, Tuple[bool, str]] = {}
    rows: List[List[str]] = []
    idx_map: List[str] = []
    for ev in events:
        fp = _fingerprint(ev)
        if fp in seen_fp or _looks_duplicate(ev, existing):
            try:
                log_action('finance_skip_duplicate', f'kind=expense', f'{ev.counterparty} ${ev.amount:.2f} on {ev.ts.date().isoformat()}')
            except Exception:
                pass
            results[ev.email_id] = (False, 'dup_skipped')
            continue
        rows.append(_build_expense_row(ev))
        idx_map.append(ev.email_id)
    if rows:
        appended = await _append_rows_with_retry(ws, rows, 'expense')
        for i, eid in enumerate(idx_map):
            results[eid] = appended[i]
    for ev in events:
        results.setdefault(ev.email_id, (False, 'dup_skipped'))
    return results


async def _notify_sandbox(
    bot,
    event: FinanceEvent,
    status: str,
    sheet_result: Tuple[bool, str],
    fallback: bool,
    blank_note: bool,
) -> None:
    ch_id = getattr(settings, 'ch_sandbox', None)
    if not ch_id:
        return
    channel = None
    try:
        channel = bot.get_channel(int(ch_id)) if bot else None
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
        body = (
            f"{provider}: {amount} from {counterparty}, message: {MESSAGE_EMPTY_SENTINEL}.\n"
            "Unsure of payment type; logged as Donations by default."
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


def _assign_income_category(event: FinanceEvent, counts: Dict[datetime.date, Counter]) -> Tuple[str, bool]:
    """Lock in category, borrowing context from prior same-day events."""
    if event.category:
        counts[event.ts.date()][event.category] += 1
        return event.category, False
    date_counts = counts[event.ts.date()]
    if date_counts:
        cat, num = date_counts.most_common(1)[0]
        if num >= 2:
            event.category = cat
            date_counts[cat] += 1
            return cat, True
    event.category = _DONATION_DEFAULT
    counts[event.ts.date()][event.category] += 1
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


async def process_financial_emails(bot) -> None:
    """Main entrypoint: read new email logs, file into Sheets, notify Discord."""
    async with FINANCE_LOCK:
        processed = _load_index()
        events: List[FinanceEvent] = []
        for email in _iter_email_logs():
            email_id = email.get('id')
            if not email_id:
                continue
            if email_id in processed:
                continue
            event, status = _classify_email(email)
            if event is None:
                _append_index({
                    "email_id": email_id,
                    "status": status,
                    "subject": email.get('subject', ''),
                })
                processed[email_id] = True
                continue
            events.append(event)
        if not events:
            return

        income_counts: Dict[datetime.date, Counter] = defaultdict(Counter)
        income_events: List[Tuple[FinanceEvent, bool]] = []
        expense_events: List[Tuple[FinanceEvent, bool]] = []
        for event in events:
            if event.direction == 'income':
                _, inferred = _assign_income_category(event, income_counts)
                income_events.append((event, inferred))
            else:
                _, inferred = _assign_expense_category(event)
                expense_events.append((event, inferred))

        income_result_map = await _append_income_rows([ev for ev, _ in income_events])
        expense_result_map = await _append_expense_rows([ev for ev, _ in expense_events])

        for event, inferred in income_events:
            sheet_res = income_result_map.get(event.email_id, (False, 'not_logged'))
            if sheet_res[0]:
                _append_index({
                    "email_id": event.email_id,
                    "status": event.direction,
                    "category": event.category,
                    "amount": event.amount,
                    "fingerprint": _fingerprint(event),
                })
                processed[event.email_id] = True
            # Skip sandbox notice for duplicates
            if not (not sheet_res[0] and str(sheet_res[1]).startswith('dup_')):
                blank = event.note.strip() == ""
                await _notify_sandbox(bot, event, event.direction, sheet_res, inferred, blank)

        for event, inferred in expense_events:
            sheet_res = expense_result_map.get(event.email_id, (False, 'not_logged'))
            if sheet_res[0]:
                _append_index({
                    "email_id": event.email_id,
                    "status": event.direction,
                    "category": event.category,
                    "amount": event.amount,
                    "fingerprint": _fingerprint(event),
                })
                processed[event.email_id] = True
            if not (not sheet_res[0] and str(sheet_res[1]).startswith('dup_')):
                blank = event.note.strip() == ""
                await _notify_sandbox(bot, event, event.direction, sheet_res, inferred, blank)
