from __future__ import annotations
import os
import sqlite3
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

# Local package imports must be relative inside "tomcat"
from ..config import settings
from ..logger import log_event, log_action
from ..services.sheets_client import sheets_client as get_client
try:
    from ..utils.sender import safe_send  # canonical signature: (ch, text) -> Awaitable[None]
except Exception:
    async def safe_send(ch, text):  # fallback
        await ch.send(text)

# Optional deps used only when Gmail ingest is enabled
# Do NOT import transformers here; it shouts about missing PyTorch/TensorFlow.
from bs4 import BeautifulSoup  # ok at import time
from rapidfuzz import fuzz      # ok at import time

DB_PATH = "dues.sqlite3"
DUES_CURRENCY = getattr(settings, "dues_currency", "USD")
GMAIL_ENABLED = bool(getattr(settings, "gmail_enabled", False))
GMAIL_CREDENTIALS_PATH = getattr(settings, "gmail_credentials_path", "")
GMAIL_TOKEN_PATH = getattr(settings, "gmail_token_path", "gmail_token.json")
GMAIL_QUERY = getattr(settings, "gmail_query", "from:(paypal.com OR cash.app OR venmo.com) newer_than:30d")

@dataclass
class Payment:
    txn_id: str
    provider: str
    amount: int  # cents
    currency: str
    payer_name: str
    payer_email: str
    payer_handle: str
    memo: str
    ts_epoch: int
    source: str
    status: str
    matched_user_id: Optional[str] = None
    match_score: Optional[float] = None

def init_db() -> None:
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

def _fetch_gmail_emails() -> list[dict]:
    if not GMAIL_ENABLED:
        return []
    # Lazy import Google libs so starting the bot doesn't require them
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = None
    if os.path.exists(GMAIL_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, scopes)
    if not creds:
        if not GMAIL_CREDENTIALS_PATH:
            return []
        flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_PATH, scopes)
        creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    svc = build("gmail", "v1", credentials=creds)
    res = svc.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=20).execute()
    ids = [m["id"] for m in res.get("messages", [])]
    out = []
    for mid in ids:
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        snippet = msg.get("snippet", "")
        out.append({"id": mid, "headers": headers, "snippet": snippet, "raw": msg})
    return out

def _parse_payment_from_email(e: dict) -> Optional[Payment]:
    snippet = e.get("snippet", "") or ""
    amt = None
    m = re.search(r"\$([0-9]+(?:\.[0-9]{2})?)", snippet)
    if m:
        amt = int(round(float(m.group(1)) * 100))
    if not amt:
        return None
    when = int(datetime.now(timezone.utc).timestamp())
    return Payment(
        txn_id=e.get("id", ""),
        provider="email",
        amount=amt,
        currency=DUES_CURRENCY,
        payer_name="Unknown",
        payer_email=e.get("headers", {}).get("from", ""),
        payer_handle="",
        memo=snippet[:200],
        ts_epoch=when,
        source=f"gmail:{when}",
        status="captured",
    )

async def handle_dues_notice(intent, ctx) -> None:
    ch = ctx["channel"]
    await safe_send(ch, "Dues notice handler is stubbed. Enable Gmail ingest or wire a real provider.")

def _open_or_create_worksheet(sh, title: str):
    try:
        return sh.worksheet(title)
    except Exception:
        # Create the worksheet with a small grid and basic headers
        try:
            ws = sh.add_worksheet(title=title, rows=100, cols=8)
            ws.append_row(["kind", "ts_iso", "status", "count"])
            return ws
        except Exception as e:
            # Bubble the original title to your log_event caller
            raise


async def process_dues_cycle(bot) -> None:
    init_db()
    emails = _fetch_gmail_emails()
    with sqlite3.connect(DB_PATH) as c:
        cur = c.cursor()
        for e in emails:
            p = _parse_payment_from_email(e)
            if not p:
                continue
            cur.execute(
                """INSERT OR IGNORE INTO payments(
                    txn_id, provider, amount_cents, currency,
                    payer_name, payer_email, payer_handle, memo,
                    ts_epoch, source, status, matched_user_id, match_score
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.txn_id, p.provider, p.amount, p.currency,
                    p.payer_name, p.payer_email, p.payer_handle, p.memo,
                    p.ts_epoch, p.source, p.status, p.matched_user_id, p.match_score
                ),
            )
        wrote = c.total_changes

    if not settings.sheet_vision_id:
        return
    gc = get_client()
    sh = gc.open_by_key(settings.sheet_vision_id)
    ws = _open_or_create_worksheet(sh, "Membership Application List")
    now = datetime.now(timezone.utc).isoformat()
    ws.append_row(["dues_cycle", now, "processed", wrote])

