from __future__ import annotations
import os
import asyncio
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
    token = _env("GMAIL_TOKEN_PATH", "gmail_token.json") or "gmail_token.json"
    return cred, token

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
        await safe_send(ch, f"Last email:\nSubject: {subject}\nFrom: {from_hdr}")
        log_event({"event": "gmail_last_email", "subject": subject, "from": from_hdr})
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

