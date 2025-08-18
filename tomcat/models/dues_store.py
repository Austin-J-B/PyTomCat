from __future__ import annotations
import sqlite3, time, os
from dataclasses import dataclass
from typing import Optional

DB_PATH = os.getenv("DUES_DB", "./dues.sqlite")

@dataclass
class Payment:
    provider: str
    txn_id: str
    amount_cents: int
    currency: str
    payer_name: str | None
    payer_handle: str | None
    payer_email: str | None
    memo: str | None
    ts_epoch: int
    raw_source: str

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS payments(
                id INTEGER PRIMARY KEY,
                provider TEXT,
                txn_id TEXT UNIQUE,
                amount_cents INTEGER,
                currency TEXT,
                payer_name TEXT,
                payer_handle TEXT,
                payer_email TEXT,
                memo TEXT,
                ts_epoch INTEGER,
                raw_source TEXT,
                matched_user_id TEXT,
                match_score REAL,
                status TEXT DEFAULT 'unreviewed',
                created_ts INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS reviews(
                id INTEGER PRIMARY KEY,
                payment_txn_id TEXT,
                notes TEXT,
                reviewer TEXT,
                ts INTEGER
            )"""
        )

def insert_payment(p: Payment) -> bool:
    with _conn() as c:
        try:
            c.execute(
                """INSERT INTO payments(
                    provider, txn_id, amount_cents, currency,
                    payer_name, payer_handle, payer_email, memo,
                    ts_epoch, raw_source, created_ts
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.provider,
                    p.txn_id,
                    p.amount_cents,
                    p.currency,
                    p.payer_name,
                    p.payer_handle,
                    p.payer_email,
                    p.memo,
                    p.ts_epoch,
                    p.raw_source,
                    int(time.time()),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

def set_match(txn_id: str, user_id: int, score: float, status: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE payments SET matched_user_id=?, match_score=?, status=? WHERE txn_id=?",
            (str(user_id), score, status, txn_id),
        )

def find_unreviewed(limit: int = 25) -> list[dict]:
    with _conn() as c:
        cur = c.execute(
            "SELECT * FROM payments WHERE status='unreviewed' ORDER BY ts_epoch DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
