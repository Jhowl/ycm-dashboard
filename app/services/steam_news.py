"""Fetch recent news/patches for a Steam app."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.logging_setup import get_logger
from app.time_utils import format_datetime_ny

logger = get_logger(__name__)

_NEWS_ENDPOINT = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/"


def fetch_steam_news(app_id: int | None, *, count: int = 5) -> list[dict[str, Any]]:
    """Return a short list of news items for a Steam app (no API key required)."""
    if not app_id:
        return []

    params = {
        "appid": int(app_id),
        "count": max(1, min(20, int(count))),
        "maxlength": 450,
        "format": "json",
    }

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(_NEWS_ENDPOINT, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("steam_news_failed", extra={"app_id": app_id, "err": str(exc)[:200]})
        return []

    items = payload.get("appnews", {}).get("newsitems", []) or []
    out: list[dict[str, Any]] = []
    for item in items:
        try:
            ts = int(item.get("date") or 0)
        except (TypeError, ValueError):
            ts = 0
        date_label = (
            format_datetime_ny(datetime.fromtimestamp(ts, timezone.utc)) if ts else None
        )
        out.append(
            {
                "gid": item.get("gid"),
                "title": item.get("title") or "Sem titulo",
                "url": item.get("url"),
                "author": item.get("author"),
                "feedlabel": item.get("feedlabel"),
                "contents_snippet": _truncate(item.get("contents"), 320),
                "date_label": date_label,
            }
        )
    return out


def _truncate(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"
