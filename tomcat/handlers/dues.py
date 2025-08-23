from __future__ import annotations
import os, sqlite3, base64, re
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from transformers import pipeline
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from tomcat.config import settings
from tomcat.logger import log_event, log_action
from tomcat.utils.sender import safe_send
from tomcat.services.sheets_client import get_client

DB_PATH = os.getenv("DUES_DB", "dues.sqlite")

@dataclass
class Payment:
    provider: str
    txn_id: str
    amount_cents: int
    currency: str
    payer_name: str | None
    payer_email: str | None
    payer_handle: str | None
    memo: str | None
    ts_epoch: int
    source: str
    status: str = "unreviewed"
    matched_user_id: int | None = None
    match_score: float | None = None


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS payments(
                txn_id TEXT PRIMARY KEY,
                provider TEXT,
                amount_cents INTEGER,
                currency TEXT,
                payer_name TEXT,
                payer_email TEXT,
                payer_handle TEXT,
                memo TEXT,
                ts_epoch INTEGER,
                source TEXT,
                status TEXT,
                matched_user_id TEXT,
                match_score REAL
            )"""
        )


def insert_payment(p: Payment):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """INSERT OR IGNORE INTO payments(txn_id,provider,amount_cents,currency,
            payer_name,payer_email,payer_handle,memo,ts_epoch,source,status,matched_user_id,match_score)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.txn_id,
                p.provider,
                p.amount_cents,
                p.currency,
                p.payer_name,
                p.payer_email,
                p.payer_handle,
                p.memo,
                p.ts_epoch,
                p.source,
                p.status,
                p.matched_user_id,
                p.match_score,
            ),
        )


def fetch_unmatched() -> List[dict]:
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute("SELECT * FROM payments WHERE status='unreviewed'")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_matched(txn_id: str, user_id: int, score: float):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE payments SET status='matched', matched_user_id=?, match_score=? WHERE txn_id=?",
            (str(user_id), score, txn_id),
        )

# ---- Gmail ingest ----

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_gmail_service_obj = None
_nlu = None


def gmail_service():
    global _gmail_service_obj
    if _gmail_service_obj:
        return _gmail_service_obj
    creds = None
    if os.path.exists(settings.gmail_token_path):
        creds = Credentials.from_authorized_user_file(settings.gmail_token_path, _SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(settings.gmail_credentials_path, _SCOPES)
        creds = flow.run_local_server(port=0)
        with open(settings.gmail_token_path, "w") as f:
            f.write(creds.to_json())
    _gmail_service_obj = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _gmail_service_obj


def _classify_email(text: str) -> str:
    global _nlu
    if _nlu is None:
        _nlu = pipeline("zero-shot-classification", model="microsoft/deberta-v3-small")
    labels = ["Donation", "Automatic_reply", "Receipt", "Brand_deal", "Other"]
    res = _nlu(text, labels)
    return res["labels"][0]


def _parse_email(sender: str, subject: str, body: str, ts_ms: int) -> Optional[Payment]:
    s = subject.lower()
    f = sender.lower()
    text = subject + "\n" + body
    if "paypal" in f or "paypal" in s:
        amt = re.search(r"\$([0-9][0-9,]*\.[0-9]{2})", text)
        txn = re.search(r"Transaction ID[: ]+([A-Z0-9]+)", text)
        payer = re.search(r"from[: ]+([A-Za-z .'-]+)", text, re.I)
        email_m = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", text)
        return Payment(
            provider="paypal",
            txn_id=txn.group(1) if txn else f"pp-{ts_ms}",
            amount_cents=int(float(amt.group(1).replace(',', ''))*100) if amt else 0,
            currency=settings.dues_currency,
            payer_name=payer.group(1).strip() if payer else None,
            payer_email=email_m.group(1).lower() if email_m else None,
            payer_handle=None,
            memo=None,
            ts_epoch=ts_ms//1000,
            source=f"gmail:{ts_ms}"
        )
    if "venmo" in f or "venmo" in s:
        amt = re.search(r"\$([0-9][0-9,]*\.[0-9]{2})", text)
        handle = re.search(r"@([A-Za-z0-9._-]+)", text)
        payer = re.search(r"([A-Za-z .'-]+) paid you", text)
        return Payment(
            provider="venmo",
            txn_id=f"venmo-{ts_ms}",
            amount_cents=int(float(amt.group(1).replace(',', ''))*100) if amt else 0,
            currency=settings.dues_currency,
            payer_name=payer.group(1).strip() if payer else None,
            payer_email=None,
            payer_handle="@"+handle.group(1) if handle else None,
            memo=None,
            ts_epoch=ts_ms//1000,
            source=f"gmail:{ts_ms}"
        )
    if "cash.app" in f or "cash app" in s:
        amt = re.search(r"\$([0-9][0-9,]*\.[0-9]{2})", text)
        handle = re.search(r"\$[A-Za-z0-9._-]+", text)
        payer = re.search(r"from[: ]+([A-Za-z .'-]+)", text)
        return Payment(
            provider="cashapp",
            txn_id=f"cash-{ts_ms}",
            amount_cents=int(float(amt.group(1).replace(',', ''))*100) if amt else 0,
            currency=settings.dues_currency,
            payer_name=payer.group(1).strip() if payer else None,
            payer_email=None,
            payer_handle=handle.group(0) if handle else None,
            memo=None,
            ts_epoch=ts_ms//1000,
            source=f"gmail:{ts_ms}"
        )
    return None


def fetch_gmail_emails():
    svc = gmail_service()
    res = svc.users().messages().list(userId="me", q=settings.gmail_query, maxResults=20).execute()
    for msg_meta in res.get("messages", []):
        msg = svc.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        sender = headers.get("from", "unknown")
        subject = headers.get("subject", "")
        ts_ms = int(msg.get("internalDate", "0"))
        parts = msg["payload"].get("parts", [])
        data = msg["payload"].get("body", {}).get("data")
        if not data:
            for p in parts:
                if p.get("mimeType", "").startswith("text/") and p.get("body", {}).get("data"):
                    data = p["body"]["data"]
                    break
        body = ""
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            if "text/html" in msg["payload"].get("mimeType", ""):
                body = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
        email_type = _classify_email(subject + "\n" + body)
        log_event({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "email_received",
            "from": sender,
            "type": email_type,
        })
        pay = _parse_email(sender, subject, body, ts_ms)
        if pay:
            insert_payment(pay)
            log_action("gmail_payment", sender, pay.txn_id)

# ---- Discord dues ----

async def record_discord_notice(message):
    payment = Payment(
        provider="discord",
        txn_id=f"disc-{message.id}",
        amount_cents=0,
        currency=settings.dues_currency,
        payer_name=message.author.display_name,
        payer_email=None,
        payer_handle=None,
        memo=message.content,
        ts_epoch=int(message.created_at.timestamp()),
        source=f"discord:{message.id}"
    )
    insert_payment(payment)
    log_action("discord_dues_notice", f"user={message.author.id}", payment.txn_id)
    await safe_send(message.channel, "Dues notice recorded.")


async def handle_dues_notice(args, ctx):
    await record_discord_notice(ctx["message"])

# ---- Matching ----

def _fetch_roster() -> List[dict]:
    if not settings.aux_spreadsheet_id:
        return []
    gc = get_client()
    sh = gc.open_by_key(settings.aux_spreadsheet_id).worksheet("Membership Application List")
    return sh.get_all_records()


def _match_payment(payment: dict, roster: List[dict]) -> tuple[Optional[int], float]:
    best_id, best_score = None, 0.0
    for row in roster:
        name_score = fuzz.token_sort_ratio(payment.get("payer_name", ""), row.get("Name", "")) / 100.0
        email_score = 1.0 if payment.get("payer_email", "").lower() == row.get("Email", "").lower() else 0.0
        handle_score = 1.0 if payment.get("payer_handle", "").lower() == row.get("Handle", "").lower() else 0.0
        score = 0.5*email_score + 0.3*name_score + 0.2*handle_score
        if score > best_score:
            best_score = score
            try:
                best_id = int(row.get("DiscordID"))
            except Exception:
                best_id = None
    return best_id, best_score

async def process_dues_cycle(bot):
    if settings.gmail_enabled:
        fetch_gmail_emails()
    roster = _fetch_roster()
    for p in fetch_unmatched():
        uid, score = _match_payment(p, roster)
        if uid and score >= 0.85:
            mark_matched(p["txn_id"], uid, score)
            line = log_action("dues_match", p["txn_id"], f"user={uid} score={score:.2f}")
            if settings.ch_logging and not settings.silent_mode:
                ch = bot.get_channel(settings.ch_logging)
                if ch:
                    await safe_send(ch, line)

