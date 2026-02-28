from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import Settings

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"


def _load_token_payload(settings: Settings) -> dict:
    token_path = Path(settings.youtube_token_file)
    if not token_path.exists():
        raise RuntimeError("YouTube token file not found. Run OAuth first.")
    try:
        return json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid YouTube token file") from exc


def _save_token_payload(settings: Settings, payload: dict) -> None:
    token_path = Path(settings.youtube_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _token_expired(payload: dict) -> bool:
    access_token = payload.get("access_token")
    if not access_token:
        return True

    obtained = payload.get("obtained_at_utc")
    expires_in = int(payload.get("expires_in", 0) or 0)
    if not obtained or expires_in <= 0:
        return False

    try:
        obtained_at = datetime.fromisoformat(str(obtained).replace("Z", "+00:00"))
    except ValueError:
        return False

    if obtained_at.tzinfo is None:
        obtained_at = obtained_at.replace(tzinfo=timezone.utc)

    expires_at = obtained_at + timedelta(seconds=max(0, expires_in - 60))
    return datetime.now(timezone.utc) >= expires_at


def _refresh_access_token(settings: Settings, payload: dict) -> dict:
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Missing refresh_token in YouTube token payload")
    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise RuntimeError("YOUTUBE_CLIENT_ID/SECRET missing for token refresh")

    data = {
        "client_id": settings.youtube_client_id,
        "client_secret": settings.youtube_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = httpx.post(GOOGLE_TOKEN_ENDPOINT, data=data, timeout=20.0)
    if response.status_code >= 400:
        raise RuntimeError(f"Failed to refresh YouTube token ({response.status_code})")

    refreshed = response.json()
    payload["access_token"] = refreshed.get("access_token")
    if refreshed.get("expires_in") is not None:
        payload["expires_in"] = refreshed.get("expires_in")
    payload["obtained_at_utc"] = datetime.now(timezone.utc).isoformat()
    _save_token_payload(settings, payload)
    return payload


def _get_valid_access_token(settings: Settings) -> str:
    payload = _load_token_payload(settings)
    if _token_expired(payload):
        payload = _refresh_access_token(settings, payload)

    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Missing access_token in YouTube token payload")
    return str(token)


def upload_video_to_youtube(
    settings: Settings,
    *,
    title: str,
    description: str,
    tags: list[str],
    visibility: str,
    video_path: str,
) -> str:
    access_token = _get_valid_access_token(settings)
    source = Path(video_path)
    if not source.exists():
        raise RuntimeError("Video file not found for upload")

    safe_title = (title or "Gameplay").strip()[:100] or "Gameplay"
    safe_description = (description or "").strip()[:5000]
    safe_tags: list[str] = []
    total_len = 0
    for tag in tags[:30]:
        t = str(tag).strip()
        if not t:
            continue
        if len(t) > 30:
            t = t[:30]
        if total_len + len(t) > 450:
            break
        safe_tags.append(t)
        total_len += len(t)

    metadata = {
        "snippet": {
            "title": safe_title,
            "description": safe_description,
            "tags": safe_tags,
            "categoryId": "20",
        },
        "status": {
            "privacyStatus": visibility if visibility in {"private", "public", "unlisted"} else "private"
        },
    }

    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"

    # Resumable upload session (avoids loading full file into memory)
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": content_type,
        "X-Upload-Content-Length": str(source.stat().st_size),
    }
    init_params = {"part": "snippet,status", "uploadType": "resumable"}
    init_resp = httpx.post(
        YOUTUBE_UPLOAD_ENDPOINT,
        params=init_params,
        headers=init_headers,
        content=json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
        timeout=30.0,
    )
    if init_resp.status_code >= 400:
        # Fallback with minimal snippet for channels that reject category/tags payloads.
        fallback_metadata = {
            "snippet": {"title": safe_title, "description": safe_description},
            "status": {
                "privacyStatus": visibility if visibility in {"private", "public", "unlisted"} else "private"
            },
        }
        init_resp = httpx.post(
            YOUTUBE_UPLOAD_ENDPOINT,
            params=init_params,
            headers=init_headers,
            content=json.dumps(fallback_metadata, ensure_ascii=False).encode("utf-8"),
            timeout=30.0,
        )

    if init_resp.status_code >= 400:
        text = init_resp.text.strip().replace("\n", " ")[:1200]
        raise RuntimeError(f"YouTube upload init failed ({init_resp.status_code}): {text}")

    upload_url = init_resp.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube resumable init missing upload Location header")

    put_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": content_type,
        "Content-Length": str(source.stat().st_size),
    }
    with source.open("rb") as fh:
        response = httpx.put(upload_url, headers=put_headers, content=fh, timeout=1800.0)

    if response.status_code >= 400:
        text = response.text.strip().replace("\n", " ")[:800]
        raise RuntimeError(f"YouTube upload failed ({response.status_code}): {text}")

    data = response.json()
    video_id = data.get("id")
    if not video_id:
        raise RuntimeError("YouTube upload succeeded but no video id returned")

    return f"https://www.youtube.com/watch?v={video_id}"
