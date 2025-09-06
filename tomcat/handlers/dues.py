from __future__ import annotations
import os
import asyncio
import json
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # type: ignore
from typing import Any, Dict, Optional

from ..logger import log_event, log_action
from ..config import settings

try:
    from ..utils.sender import safe_send  # (channel, content, **kwargs)
except Exception:
    async def safe_send(ch, text, **kwargs):
        await ch.send(text, **kwargs)

# ---- Gmail auth helpers ------------------------------------------------------

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(key)
    return v if v is not None else default

def _paths() -> tuple[str, str]:
    cred = _env("GMAIL_CREDENTIALS_PATH", "credentials/gmail_oauth_client.json") or "credentials/gmail_oauth_client.json"
    # Default token now lives under credentials/
    token = _env("GMAIL_TOKEN_PATH", "credentials/gmail_token.json") or "credentials/gmail_token.json"
    return cred, token

def _maybe_migrate_token(target_path: str) -> str:
    """If an old token file exists at ./gmail_token.json and the target does not, move it.
    Returns the path that should be used after migration.
    """
    try:
        old = "gmail_token.json"
        if not os.path.exists(target_path) and os.path.exists(old):
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
            import shutil
            shutil.move(old, target_path)
        return target_path
    except Exception:
        return target_path

_PENDING_OAUTH: Dict[int, Any] = {}

def _new_flow():
    from google_auth_oauthlib.flow import InstalledAppFlow
    cred_path, _ = _paths()
    flow = InstalledAppFlow.from_client_secrets_file(cred_path, scopes=GMAIL_SCOPES)
    return flow


async def _build_gmail_service(channel) -> Any:
    """Build a Gmail service; if auth is needed, post the URL and wait for approval via Discord code."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request

    cred_path, token_path = _paths()
    token_path = _maybe_migrate_token(token_path)
    creds: Optional[Credentials] = None  # type: ignore

    # Load existing token if present
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
        except Exception:
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            await asyncio.to_thread(creds.refresh, Request())
        except Exception:
            creds = None

    # Fresh authorization if needed
    if not creds:
        if not os.path.exists(cred_path):
            raise FileNotFoundError(f"Missing OAuth client file at {cred_path}")
        # Start a manual flow and ask the admin to paste the code or full redirect URL back in Discord
        flow = _new_flow()
        port = int(os.getenv("GMAIL_LOCAL_PORT", "8765") or "8765")
        flow.redirect_uri = f"http://localhost:{port}/"
        auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        _PENDING_OAUTH[int(getattr(getattr(channel, 'guild', None), 'id', 0)) or 0] = flow  # also stash globally per guild
        _PENDING_OAUTH[-1] = flow  # fallback/global
        try:
            await safe_send(channel, (
                "Gmail authorization needed. Open and approve, then reply: 'TomCat, auth code <code>' or 'TomCat, auth url <full URL>'.\n"
                f"URL:\n{auth_url}"
            ))
            log_action("gmail_auth_url", "", auth_url)
        except Exception:
            pass
        raise RuntimeError("gmail_auth_pending")

    # Build the service
    return await asyncio.to_thread(lambda: build("gmail", "v1", credentials=creds))


# ----- time helpers -----------------------------------------------------------
def _now_iso() -> str:
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(getattr(settings, "timezone", "America/Chicago"))
        except Exception:
            tz = None
    now = datetime.now(tz) if tz else datetime.now()
    return now.isoformat()


# ---- Public handler ----------------------------------------------------------

async def handle_check_last_email(intent, ctx) -> None:
    """Admin test: fetch the most recent email's Subject and From and post it.
    Honors silent mode via safe_send. Logs actions to human/machine logs.
    """
    ch = ctx["channel"]
    try:
        svc = await _build_gmail_service(ch)
        # Fetch latest received (not sent) message metadata only
        query = os.getenv("GMAIL_LAST_QUERY", "in:inbox -from:me")
        res = await asyncio.to_thread(
            lambda: svc.users().messages().list(userId="me", q=query, maxResults=1, includeSpamTrash=False).execute()
        )
        msgs = res.get("messages", []) if isinstance(res, dict) else []
        if not msgs:
            await safe_send(ch, "No received messages found.")
            log_action("gmail_no_messages", "", "empty")
            return
        mid = msgs[0].get("id")
        msg = await asyncio.to_thread(
            lambda: svc.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From"]).execute()
        )
        payload = msg.get("payload", {}) if isinstance(msg, dict) else {}
        headers = {h.get("name", ""): h.get("value", "") for h in (payload.get("headers", []) or [])}
        subject = headers.get("Subject", "(no subject)")
        from_hdr = headers.get("From", "(unknown sender)")
        snippet = msg.get("snippet", "") if isinstance(msg, dict) else ""
        await safe_send(ch, f"Last email:\nSubject: {subject}\nFrom: {from_hdr}")
        log_event({
            "event": "gmail_last_email",
            "subject": subject,
            "from": from_hdr,
            "snippet": snippet,
        })
    except FileNotFoundError as e:
        await safe_send(ch, f"Gmail not configured: {e}")
        log_action("gmail_error", "config", str(e))
    except RuntimeError as e:
        # Likely gmail_auth_pending
        log_action("gmail_pending", "", str(e))
    except Exception as e:
        await safe_send(ch, f"Gmail error: {e}")
        log_action("gmail_error", type(e).__name__, str(e))


async def handle_gmail_auth_code(intent, ctx) -> None:
    """Complete the OAuth flow using a pasted code or full redirect URL from Discord."""
    ch = ctx["channel"]
    user = ctx.get("author")
    raw = (intent.data or {}).get("auth") or ""
    try:
        from urllib.parse import urlparse, parse_qs
        # Extract code from either a direct code or a full redirect URL
        code = raw.strip()
        if code.startswith("http"):
            qs = parse_qs(urlparse(code).query)
            code = (qs.get("code") or [""])[0]
        if not code:
            await safe_send(ch, "Could not find an authorization code. Please paste the code or the full redirect URL.")
            return
        # Get the pending flow (prefer guild key, else global)
        flow = _PENDING_OAUTH.get(int(getattr(getattr(ch, 'guild', None), 'id', 0)) or 0) or _PENDING_OAUTH.get(-1)
        if not flow:
            # Start a new flow if none pending
            flow = _new_flow()
        # Use the same redirect URI convention
        port = int(os.getenv("GMAIL_LOCAL_PORT", "8765") or "8765")
        flow.redirect_uri = f"http://localhost:{port}/"
        # Exchange code for tokens
        await asyncio.to_thread(flow.fetch_token, code=code)
        # Save token
        _, token_path = _paths()
        token_path = _maybe_migrate_token(token_path)
        # Ensure directory exists before writing
        try:
            os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
        except Exception:
            pass
        try:
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(flow.credentials.to_json())
        except Exception:
            pass
        # Clear pending
        try:
            _PENDING_OAUTH.pop(int(getattr(getattr(ch, 'guild', None), 'id', 0)) or 0, None)
            _PENDING_OAUTH.pop(-1, None)
        except Exception:
            pass
        await safe_send(ch, "Gmail authorized. You can now run: 'TomCat, check the last email'.")
        log_action("gmail_auth_complete", f"by={getattr(user,'id',0)}", "ok")
    except Exception as e:
        await safe_send(ch, f"Auth error: {e}")
        log_action("gmail_auth_error", type(e).__name__, str(e))


# ---- Email logging (periodic + manual) --------------------------------------

import base64
from typing import List, Dict
from bs4 import BeautifulSoup  # type: ignore

EMAILS_DIR = "logs/emails"
INDEX_FILE = f"{EMAILS_DIR}/index.jsonl"
_EMAIL_LOG_LOCK = asyncio.Lock()

def _ensure_email_dirs():
    try:
        os.makedirs(EMAILS_DIR, exist_ok=True)
    except Exception:
        pass

def _load_logged_ids() -> set[str]:
    _ensure_email_dirs()
    ids: set[str] = set()
    try:
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        mid = str(obj.get("id", ""))
                        if mid:
                            ids.add(mid)
                    except Exception:
                        continue
    except Exception:
        pass
    return ids

def _append_index(mid: str, seen: set[str] | None = None):
    _ensure_email_dirs()
    try:
        # avoid duplicates in index file
        if seen is None:
            # light-weight read to skip duplicate writes
            existing = _load_logged_ids()
            if mid in existing:
                return
        with open(INDEX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": mid, "logged_at": _now_iso()}) + "\n")
    except Exception:
        pass

def _decode_part(data: str) -> str:
    try:
        # Gmail uses base64url
        raw = base64.urlsafe_b64decode(data.encode("utf-8"))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _extract_text_content(msg: Dict[str, Any]) -> str:
    # Prefer text/plain; fallback to text/html stripped; else snippet
    payload = msg.get("payload") or {}
    def _walk(p) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not isinstance(p, dict):
            return out
        parts = p.get("parts") or []
        for part in parts:
            out.append(part)
            out.extend(_walk(part))
        return out
    parts = _walk(payload)
    # Single-part messages may put body directly on payload
    if not parts:
        parts = [payload]
    text = ""
    html = ""
    for part in parts:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        data = body.get("data")
        if not data:
            continue
        if mime.startswith("text/plain"):
            text = _decode_part(data)
            break
        if mime.startswith("text/html") and not html:
            html = _decode_part(data)
    if not text and html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator="\n")
        except Exception:
            text = html
    if not text:
        text = msg.get("snippet", "")
    return text or ""

_MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec",
}

async def _write_email_log_row(obj: Dict[str, Any]):
    _ensure_email_dirs()
    # Write into monthly NDJSON: e.g., 2025-Sept.ndjson
    ts = obj.get("ts_received") or obj.get("ts_logged") or _now_iso()
    try:
        # Parse to get year and month; handle Z timezone suffix
        ts_clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_clean)
    except Exception:
        dt = datetime.now()
    mon_name = _MONTH_NAMES.get(dt.month, f"{dt.month:02d}")
    path = os.path.join(EMAILS_DIR, f"{dt.year}-{mon_name}.ndjson")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

async def _log_emails_batch(svc, messages: List[Dict[str, Any]], delay_sec: float = 10.0) -> int:
    """Fetch full messages and append to logs/emails/*.ndjson for any not yet logged.
    Returns count logged.
    """
    async with _EMAIL_LOG_LOCK:
        # Build a working set of already logged IDs and de-duplicate input
        logged = _load_logged_ids()
        seen: set[str] = set(logged)
        uniq: List[Dict[str, Any]] = []
        for m in messages:
            mid = str(m.get("id"))
            if not mid:
                continue
            if mid in {str(x.get("id")) for x in uniq}:
                continue
            uniq.append(m)

        count = 0
        total = len(uniq)
        for m in uniq:
            mid = str(m.get("id"))
            if not mid or mid in seen:
                continue
            full = await asyncio.to_thread(lambda: svc.users().messages().get(userId="me", id=mid, format="full").execute())
            payload = full.get("payload", {})
            headers = {h.get("name", ""): h.get("value", "") for h in (payload.get("headers", []) or [])}
            subject = headers.get("Subject", "(no subject)")
            from_hdr = headers.get("From", "(unknown sender)")
            internal_date_ms = int(full.get("internalDate", 0)) if str(full.get("internalDate", "")).isdigit() else 0
            ts_received = datetime.utcfromtimestamp(internal_date_ms/1000).isoformat() + "Z" if internal_date_ms else None
            content = _extract_text_content(full)
            row = {
                "event": "email_received",
                "id": mid,
                "subject": subject,
                "from": from_hdr,
                "ts_received": ts_received,
                "ts_logged": _now_iso(),
                "content": content,
            }
            await _write_email_log_row(row)
            _append_index(mid, seen)
            seen.add(mid)
            count += 1
            if delay_sec and count < total:
                await asyncio.sleep(delay_sec)
        return count

async def start_gmail_logging_scheduler(bot) -> None:
    """Every ~4 hours, log any newly received emails in the last 4 hours."""
    while True:
        try:
            async with _EMAIL_LOG_LOCK:
                # Prefer logging channel for auth prompts if needed
                ch = None
                try:
                    from ..config import settings as _settings
                    ch_id = getattr(_settings, "ch_logging", None)
                    if ch_id:
                        ch = bot.get_channel(int(ch_id))
                except Exception:
                    ch = None
                svc = await _build_gmail_service(ch or getattr(bot, "user", None))
                # 4h window; exclude sent mail
                q = "in:inbox -from:me newer_than:4h"
                res = await asyncio.to_thread(lambda: svc.users().messages().list(userId="me", q=q, maxResults=100, includeSpamTrash=False).execute())
                msgs = res.get("messages", []) if isinstance(res, dict) else []
                if msgs:
                    n = await _log_emails_batch(svc, msgs, delay_sec=10.0)
                    log_action("gmail_log_scheduler", f"found={len(msgs)}", f"logged={n}")
        except RuntimeError:
            # likely gmail_auth_pending; do nothing until authorized
            log_action("gmail_log_scheduler", "auth", "pending")
        except Exception as e:
            log_action("gmail_log_scheduler_error", "", str(e))
        # Sleep ~4 hours
        await asyncio.sleep(4 * 60 * 60)

async def handle_log_recent_emails(intent, ctx) -> None:
    """Manual: TomCat, log the past N emails (received)."""
    ch = ctx["channel"]
    try:
        n = int(intent.data.get("count") or 10)
        async with _EMAIL_LOG_LOCK:
            svc = await _build_gmail_service(ch)
            q = os.getenv("GMAIL_LAST_QUERY", "in:inbox -from:me")
            res = await asyncio.to_thread(lambda: svc.users().messages().list(userId="me", q=q, maxResults=n, includeSpamTrash=False).execute())
            msgs = res.get("messages", []) if isinstance(res, dict) else []
            if not msgs:
                await safe_send(ch, "No emails found to log.")
                return
            # Compute how many of these are already logged
            existing = _load_logged_ids()
            candidates = [m for m in msgs if str(m.get("id")) and str(m.get("id")) not in existing]
            # Log in reverse chronological so logs read smoothly
            logged = await _log_emails_batch(svc, list(msgs)[::-1], delay_sec=2.0)
        already = max(0, len(candidates) - logged)
        suffix = f" (skipped {already} already logged)" if already else ""
        await safe_send(ch, f"Logged {logged} email(s){suffix}.")
        log_action("gmail_log_manual", f"req={n}", f"logged={logged}; skipped={already}")
    except RuntimeError:
        log_action("gmail_log_manual", "auth", "pending")
    except Exception as e:
        await safe_send(ch, f"Gmail error: {e}")
        log_action("gmail_log_manual_error", "", str(e))

