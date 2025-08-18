from __future__ import annotations
import base64
from typing import Any
from .gmail_client import get_service
from .dues_parser import parse_payment_email
from .dues_store import insert_payment
from tomcat.config import settings

def poll_gmail_once() -> None:
    svc = get_service()
    res = (
        svc.users()
        .messages()
        .list(userId="me", q=settings.gmail_query, maxResults=50)
        .execute()
    )
    for m in res.get("messages", []):
        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=m["id"], format="full")
            .execute()
        )
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        ts_ms = int(msg.get("internalDate", "0"))
        data = None
        payload = msg.get("payload", {})
        if payload.get("body", {}).get("data"):
            data = payload["body"]["data"]
        else:
            for part in payload.get("parts", []):
                if part.get("mimeType", "").startswith("text/") and part.get("body", {}).get("data"):
                    data = part["body"]["data"]
                    break
        if not data:
            continue
        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        payment = parse_payment_email(sender, subject, body, ts_ms)
        if payment:
            insert_payment(payment)
