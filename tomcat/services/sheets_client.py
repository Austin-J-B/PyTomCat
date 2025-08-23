"""Google Sheets client via gspread.
Share your sheets with the service account email.
"""
from __future__ import annotations
from gspread.auth import service_account
from ..config import settings  # package-local config

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client = None

def sheets_client():
    global _client
    if _client:
        return _client
    path = getattr(settings, "google_service_account_json", None) or getattr(settings, "google_sa_json", "credentials/service_account.json")
    _client = service_account(filename=path, scopes=_SCOPES)
    return _client