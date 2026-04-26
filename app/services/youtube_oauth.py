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


def clear_token(settings: Settings) -> bool:
    """Remove the saved YouTube token file. Returns True if the file existed."""
    token_path = Path(settings.youtube_token_file)
    if not token_path.exists():
        return False
    token_path.unlink()
    return True


def validate_token(settings: Settings) -> dict:
    """Check whether the saved YouTube token is valid.

    Strategy:
    1. If no token file exists -> ok=False, reason="missing".
    2. Decode payload. If access_token is expired and we have a refresh_token,
       try to refresh it (pulls fresh access_token).
    3. Hit Google ``/oauth2/v3/tokeninfo`` for scope/audience proof.
    4. Hit YouTube Data API ``channels?part=snippet&mine=true`` to confirm
       the upload scope is actually usable and return the channel handle.
    """
    token_path = Path(settings.youtube_token_file)
    if not token_path.exists():
        return {"ok": False, "reason": "missing", "channel": None}

    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": f"invalid_token_file: {exc}", "channel": None}

    # Lazy import to avoid circular import (youtube_publish imports from this module too).
    from app.services.youtube_publish import _refresh_access_token, _token_expired

    if _token_expired(payload):
        if not payload.get("refresh_token"):
            return {"ok": False, "reason": "expired_no_refresh", "channel": None}
        try:
            payload = _refresh_access_token(settings, payload)
        except Exception as exc:  # pragma: no cover — network
            return {"ok": False, "reason": f"refresh_failed: {exc}", "channel": None}

    access_token = payload.get("access_token")
    if not access_token:
        return {"ok": False, "reason": "no_access_token", "channel": None}

    # Probe tokeninfo
    try:
        with httpx.Client(timeout=10.0) as client:
            info_resp = client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": access_token},
            )
            if info_resp.status_code >= 400:
                return {
                    "ok": False,
                    "reason": f"tokeninfo_status_{info_resp.status_code}",
                    "channel": None,
                }
            info = info_resp.json()
            scopes = (info.get("scope") or "").split()
            if YOUTUBE_UPLOAD_SCOPE not in scopes:
                return {
                    "ok": False,
                    "reason": f"missing_scope: have={','.join(scopes) or 'none'}",
                    "channel": None,
                    "scopes": scopes,
                }

            # Probe channel
            ch_resp = client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"part": "snippet", "mine": "true"},
            )
            channel = None
            if ch_resp.status_code == 200:
                items = (ch_resp.json() or {}).get("items") or []
                if items:
                    snip = items[0].get("snippet") or {}
                    channel = {
                        "title": snip.get("title"),
                        "id": items[0].get("id"),
                        "custom_url": snip.get("customUrl"),
                    }
            return {
                "ok": True,
                "reason": "valid",
                "channel": channel,
                "scopes": scopes,
                "expires_in": int(payload.get("expires_in") or 0),
            }
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": f"network_error: {exc}", "channel": None}
