from __future__ import annotations
import re
from dataclasses import dataclass
from bs4 import BeautifulSoup
from .dues_store import Payment
from tomcat.config import settings

USD = settings.dues_currency

def _amount_to_cents(text: str) -> int:
    m = re.search(r"([0-9]+)(?:[.,]([0-9]{2}))?", text.replace(",", ""))
    if not m:
        return 0
    return int(m.group(1)) * 100 + int(m.group(2) or 0)

def parse_payment_email(sender: str, subject: str, body: str, ts_ms: int) -> Payment | None:
    s = subject.lower()
    f = sender.lower()
    b = body
    # Remove HTML tags if any
    if "<" in b:
        b = BeautifulSoup(b, "html.parser").get_text(" ", strip=True)
    low = b.lower()

    if "paypal" in f or "paypal" in s:
        amt = re.search(r"\$[0-9][0-9,]*\.[0-9]{2}", b)
        txn = re.search(r"Transaction ID[: ]+([A-Z0-9]+)", b, re.I)
        payer = re.search(r"from[: ]+([^\n]+)", b, re.I)
        email = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", b)
        return Payment(
            provider="paypal",
            txn_id=(txn.group(1) if txn else f"pp-{ts_ms}"),
            amount_cents=_amount_to_cents(amt.group(0)) if amt else 0,
            currency=USD,
            payer_name=payer.group(1).strip() if payer else None,
            payer_handle=None,
            payer_email=email.group(1).lower() if email else None,
            memo=None,
            ts_epoch=ts_ms // 1000,
            raw_source=f"gmail:{ts_ms}",
        )

    if "venmo" in f or "venmo" in s:
        amt = re.search(r"\$[0-9][0-9,]*\.[0-9]{2}", b)
        handle = re.search(r"@([A-Za-z0-9._-]+)", b)
        payer = re.search(r"([A-Za-z .'-]+) paid you", low)
        memo = re.search(r"note[: ]+([^\n]+)", b, re.I)
        return Payment(
            provider="venmo",
            txn_id=f"venmo-{ts_ms}",
            amount_cents=_amount_to_cents(amt.group(0)) if amt else 0,
            currency=USD,
            payer_name=payer.group(1).strip() if payer else None,
            payer_handle=f"@{handle.group(1)}" if handle else None,
            payer_email=None,
            memo=memo.group(1).strip() if memo else None,
            ts_epoch=ts_ms // 1000,
            raw_source=f"gmail:{ts_ms}",
        )

    if "cash.app" in f or "cash app" in s:
        amt = re.search(r"\$[0-9][0-9,]*\.[0-9]{2}", b)
        handle = re.search(r"\$[A-Za-z0-9._-]+", b)
        payer = re.search(r"from[: ]+([^\n]+)", b, re.I)
        return Payment(
            provider="cashapp",
            txn_id=f"cash-{ts_ms}",
            amount_cents=_amount_to_cents(amt.group(0)) if amt else 0,
            currency=USD,
            payer_name=payer.group(1).strip() if payer else None,
            payer_handle=handle.group(0) if handle else None,
            payer_email=None,
            memo=None,
            ts_epoch=ts_ms // 1000,
            raw_source=f"gmail:{ts_ms}",
        )

    return None
