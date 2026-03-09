"""Generic Gmail ingestion: Auth, Logging, and Scheduler."""
from __future__ import annotations
import os
import asyncio
import json
import base64
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup

from ..config import settings
from ..logger import log_action, log_event
from ..utils.sender import safe_send
from . import finance  #Pass emails to finance handler

#--- Constants & Config ---
EMAILS_DIR = "logs/emails"
INDEX_FILE = f"{EMAILS_DIR}/index.jsonl"
_EMAIL_LOG_LOCK = asyncio.Lock()

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

_PENDING_OAUTH: Dict[int, Any] = {}
_PENDING_POST_AUTH: Dict[int, Dict[str, Any]] = {}
_MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec",
}

#--- Auth Helpers ---
def _paths() -> tuple[str, str]:
    cred = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials/gmail_oauth_client.json")
    token = os.getenv("GMAIL_TOKEN_PATH", "credentials/gmail_token.json")
    return cred, token

def _maybe_migrate_token(target_path: str) -> str:
    try:
        old = "gmail_token.json"
        if not os.path.exists(target_path) and os.path.exists(old):
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
            import shutil
            shutil.move(old, target_path)
        return target_path
    except Exception:
        return target_path

def _new_flow():
    from google_auth_oauthlib.flow import InstalledAppFlow
    cred_path, _ = _paths()
    flow = InstalledAppFlow.from_client_secrets_file(cred_path, scopes=GMAIL_SCOPES)
    return flow


def _oauth_key(channel) -> int:
    try:
        return int(getattr(getattr(channel, 'guild', None), 'id', 0) or 0)
    except Exception:
        return 0


def _set_post_auth_action(channel, action: Dict[str, Any]) -> None:
    key = _oauth_key(channel)
    payload = dict(action or {})
    _PENDING_POST_AUTH[key] = payload
    _PENDING_POST_AUTH[-1] = payload


def _pop_post_auth_action(channel) -> Optional[Dict[str, Any]]:
    key = _oauth_key(channel)
    action = _PENDING_POST_AUTH.pop(key, None)
    fallback = _PENDING_POST_AUTH.pop(-1, None)
    return action or fallback

async def _build_gmail_service(channel) -> Any:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request

    cred_path, token_path = _paths()
    token_path = _maybe_migrate_token(token_path)
    creds: Optional[Credentials] = None

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

    if not creds:
        if not os.path.exists(cred_path):
            raise FileNotFoundError(f"Missing OAuth client file at {cred_path}")
        flow = _new_flow()
        port = int(os.getenv("GMAIL_LOCAL_PORT", "8765") or "8765")
        flow.redirect_uri = f"http://localhost:{port}/"
        auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        #Store flow for callback
        gid = _oauth_key(channel)
        _PENDING_OAUTH[gid] = flow
        _PENDING_OAUTH[-1] = flow
        try:
            await safe_send(channel, (
                "Gmail Authorization Needed\n\n"
                "Follow these steps:\n"
                "1. Click the link below to open Google's authorization page\n"
                "2. Sign in with the organization's Gmail account\n"
                "3. Click Continue and then Authorize when prompted\n"
                "4. You'll be redirected to a blank page or an 'error' page — this is expected!\n"
                "5. Copy the entire URL from your browser's address bar\n"
                "6. Reply here with: `TomCat, auth url <paste the full URL here>`\n\n"
                f"Authorization Link:\n{auth_url}"
            ))
            log_action("gmail_auth_url", "", auth_url)
        except Exception:
            pass
        raise RuntimeError("gmail_auth_pending")

    return await asyncio.to_thread(lambda: build("gmail", "v1", credentials=creds))

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

#--- Logging Helpers ---
def _ensure_email_dirs():
    os.makedirs(EMAILS_DIR, exist_ok=True)

def _load_logged_ids() -> set[str]:
    _ensure_email_dirs()
    ids: set[str] = set()
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            obj = json.loads(line)
                            if obj.get("id"): ids.add(str(obj["id"]))
                        except: pass
        except: pass
    return ids

def _append_index(mid: str, seen: set[str] | None = None):
    _ensure_email_dirs()
    try:
        with open(INDEX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": mid, "logged_at": _now_iso()}) + "\n")
    except: pass

def _decode_part(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data.encode("utf-8"))
        return raw.decode("utf-8", errors="replace")
    except: return ""

def _extract_text_content(msg: Dict[str, Any]) -> str:
    payload = msg.get("payload") or {}
    
    def _walk(p):
        out = []
        if not isinstance(p, dict): return out
        parts = p.get("parts") or []
        for part in parts:
            out.append(part)
            out.extend(_walk(part))
        return out

    parts = _walk(payload) or [payload]
    text, html = "", ""
    for part in parts:
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if not data: continue
        if mime.startswith("text/plain"):
            text = _decode_part(data); break
        if mime.startswith("text/html") and not html:
            html = _decode_part(data)
    
    if not text and html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator="\n")
        except: text = html
    return text or msg.get("snippet", "") or ""

def _sanitize_for_ndjson(s: str) -> str:
    s = str(s) if s else ""
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")

async def _write_email_log_row(obj: Dict[str, Any]):
    _ensure_email_dirs()
    ts = obj.get("ts_received") or obj.get("ts_logged") or _now_iso()
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except:
        dt = datetime.now(timezone.utc)
    mon = _MONTH_NAMES.get(dt.month, f"{dt.month:02d}")
    path = os.path.join(EMAILS_DIR, f"{dt.year}-{mon}.ndjson")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

async def _log_emails_batch(svc, messages: List[Dict[str, Any]], delay_sec: float = 0.25) -> int:
    logged = _load_logged_ids()
    seen = set(logged)
    count = 0
    #Process newest to oldest (if input is newest-first, we reverse it outside or just process)
    #Actually, unique list first
    uniq = []
    seen_in = set()
    for m in messages:
        mid = str(m.get("id"))
        if mid and mid not in seen_in:
            uniq.append(m); seen_in.add(mid)
    
    for m in uniq:
        if str(m.get("id")) in seen: continue
        try:
            mid = str(m.get("id"))
            full = await asyncio.to_thread(lambda: svc.users().messages().get(userId="me", id=mid, format="full").execute())
            payload = full.get("payload", {})
            headers = {h.get("name"): h.get("value") for h in payload.get("headers", [])}
            ts_ms = int(full.get("internalDate", 0) or 0)
            ts_rec = datetime.fromtimestamp(ts_ms/1000, timezone.utc).isoformat().replace("+00:00", "Z") if ts_ms else None
            
            row = {
                "event": "email_received", "id": mid,
                "subject": _sanitize_for_ndjson(headers.get("Subject", "")),
                "from": _sanitize_for_ndjson(headers.get("From", "")),
                "ts_received": ts_rec, "ts_logged": _now_iso(),
                "content": _sanitize_for_ndjson(_extract_text_content(full))
            }
            await _write_email_log_row(row)
            _append_index(mid, seen); seen.add(mid)
            count += 1
            if delay_sec: await asyncio.sleep(delay_sec)
        except Exception as e:
            log_action("gmail_log_error", str(m.get("id")), str(e))
    return count

#--- Handlers ---

async def handle_check_last_email(intent, ctx) -> None:
    ch = ctx["channel"]
    try:
        _set_post_auth_action(ch, {"type": "gmail_check_last"})
        svc = await _build_gmail_service(ch)
        q = os.getenv("GMAIL_LAST_QUERY", "in:inbox -from:me")
        res = await asyncio.to_thread(lambda: svc.users().messages().list(userId="me", q=q, maxResults=1).execute())
        msgs = res.get("messages", [])
        if not msgs:
            await safe_send(ch, "No received messages found.")
            return
        msg = await asyncio.to_thread(lambda: svc.users().messages().get(userId="me", id=msgs[0]['id'], format="metadata").execute())
        headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
        await safe_send(ch, f"Last email:\nSubject: {headers.get('Subject')}\nFrom: {headers.get('From')}")
        _pop_post_auth_action(ch)
    except RuntimeError as e:
        if str(e) != "gmail_auth_pending":
            await safe_send(ch, f"Gmail error: {e}")
    except Exception as e:
        await safe_send(ch, f"Gmail error: {e}")

async def handle_gmail_auth_code(intent, ctx) -> None:
    ch = ctx["channel"]
    raw = (intent.data or {}).get("auth") or ""
    try:
        from urllib.parse import urlparse, parse_qs
        code = raw.strip()
        if code.startswith("http"):
            code = parse_qs(urlparse(code).query).get("code", [""])[0]
        if not code:
            await safe_send(ch, "No code found.")
            return
        flow = _PENDING_OAUTH.get(_oauth_key(ch)) or _PENDING_OAUTH.get(-1) or _new_flow()
        port = int(os.getenv("GMAIL_LOCAL_PORT", "8765"))
        flow.redirect_uri = f"http://localhost:{port}/"
        await asyncio.to_thread(flow.fetch_token, code=code)
        _, token_path = _paths()
        token_path = _maybe_migrate_token(token_path)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(flow.credentials.to_json())
        try:
            _PENDING_OAUTH.pop(_oauth_key(ch), None)
            _PENDING_OAUTH.pop(-1, None)
        except Exception:
            pass
        await safe_send(ch, "Gmail authorized.")
        action = _pop_post_auth_action(ch)
        if action:
            action_type = str(action.get("type") or "").strip().lower()
            if action_type == "gmail_log_recent":
                count = int(action.get("count") or 10)
                await safe_send(ch, f"Continuing email check ({count})...")
                await handle_log_recent_emails(type("Intent", (), {"data": {"count": count}})(), ctx)
            elif action_type == "gmail_check_last":
                await safe_send(ch, "Continuing last-email check...")
                await handle_check_last_email(type("Intent", (), {"data": {}})(), ctx)
    except Exception as e:
        await safe_send(ch, f"Auth error: {e}")

async def handle_log_recent_emails(intent, ctx) -> None:
    ch = ctx["channel"]
    n = int((intent.data or {}).get("count") or 10)
    await safe_send(ch, "Scanning emails...")
    async with _EMAIL_LOG_LOCK:
        try:
            _set_post_auth_action(ch, {"type": "gmail_log_recent", "count": n})
            svc = await _build_gmail_service(ch)
            res = await asyncio.to_thread(lambda: svc.users().messages().list(userId="me", q="in:inbox -from:me", maxResults=n).execute())
            msgs = res.get("messages", [])
            logged = await _log_emails_batch(svc, msgs, delay_sec=0.1)
            await safe_send(ch, f"Logged {logged} new email(s).")
            #Trigger finance
            bot = ctx.get("bot")
            if bot:
                await finance.process_financial_emails(bot)
            _pop_post_auth_action(ch)
        except RuntimeError as e:
            if str(e) != "gmail_auth_pending":
                await safe_send(ch, f"Error: {e}")
        except Exception as e:
            await safe_send(ch, f"Error: {e}")

#--- Scheduler ---

async def start_gmail_logging_scheduler(bot) -> None:
    """Independent Gmail Poller."""
    while True:
        try:
            #Try acquire lock; if busy (manual run), skip
            try:
                await asyncio.wait_for(_EMAIL_LOG_LOCK.acquire(), timeout=0.5)
                try:
                    #Check for auth config but don't crash if missing
                    if os.getenv("GMAIL_CREDENTIALS_PATH") or os.path.exists("credentials/gmail_oauth_client.json"):
                        svc = await _build_gmail_service(getattr(bot, "user", None)) #Silent auth check
                        q = "in:inbox -from:me newer_than:4h"
                        res = await asyncio.to_thread(lambda: svc.users().messages().list(userId="me", q=q, maxResults=50).execute())
                        msgs = res.get("messages", [])
                        if msgs:
                            n = await _log_emails_batch(svc, msgs[::-1], delay_sec=5.0)
                            if n > 0:
                                log_action("gmail_poller", "new_emails", f"count={n}")
                                await finance.process_financial_emails(bot)
                finally:
                    _EMAIL_LOG_LOCK.release()
            except asyncio.TimeoutError:
                pass #Lock busy
        except Exception as e:
            log_action("gmail_poller_error", "", str(e))
        
        await asyncio.sleep(4 * 60 * 60) #4 hours
