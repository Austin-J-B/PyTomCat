"""Google Sheets client via gspread.
Share your sheets with the service account email.
"""
from __future__ import annotations
from gspread.auth import service_account
from . .config import settings  # relative import up one level

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client = None

def sheets_client():
    global _client
    if _client:
        return _client
    _client = service_account(filename=settings.google_sa_json, scopes=_SCOPES)
    return _client