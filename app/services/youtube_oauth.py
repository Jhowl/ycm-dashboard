from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import Settings

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(24)


def build_youtube_auth_url(settings: Settings, state: str) -> str:
    if not settings.youtube_client_id:
        raise ValueError("YOUTUBE_CLIENT_ID is not configured")

    query = urlencode(
        {
            "client_id": settings.youtube_client_id,
            "redirect_uri": settings.youtube_redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_UPLOAD_SCOPE,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTH_ENDPOINT}?{query}"


def exchange_code_for_tokens(settings: Settings, code: str) -> dict:
    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise ValueError("YOUTUBE_CLIENT_ID or YOUTUBE_CLIENT_SECRET is missing")

    payload = {
        "code": code,
        "client_id": settings.youtube_client_id,
        "client_secret": settings.youtube_client_secret,
        "redirect_uri": settings.youtube_redirect_uri,
        "grant_type": "authorization_code",
    }

    response = httpx.post(GOOGLE_TOKEN_ENDPOINT, data=payload, timeout=20.0)
    if response.status_code >= 400:
        text = response.text.strip().replace("\n", " ")[:500]
        raise RuntimeError(f"Google token exchange failed ({response.status_code}): {text}")

    token_payload = response.json()
    token_payload["obtained_at_utc"] = datetime.now(timezone.utc).isoformat()
    return token_payload


def save_token_payload(settings: Settings, token_payload: dict) -> Path:
    token_file = Path(settings.youtube_token_file)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(token_payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return token_file
