from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings
from app.time_utils import format_datetime_ny

_STEAM_CACHE: dict[str, object] = {
    "expires_at": datetime.fromtimestamp(0, timezone.utc),
    "data": None,
}
_STEAM_RECENT_GAMES_CACHE: dict[str, object] = {
    "expires_at": datetime.fromtimestamp(0, timezone.utc),
    "data": [],
}


def _format_utc_datetime(value: datetime) -> str:
    return format_datetime_ny(value)


def _format_minutes_label(minutes: int) -> str:
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    remainder = minutes % 60
    if remainder == 0:
        return f"{hours}h"
    return f"{hours}h {remainder}m"


def _default_payload() -> dict:
    return {
        "enabled": False,
        "error": None,
        "profile": None,
        "recent_games": [],
        "recent_achievements": [],
        "last_updated_label": None,
    }


def _fetch_profile(client: httpx.Client, settings: Settings) -> dict | None:
    response = client.get(
        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
        params={
            "key": settings.steam_api_key,
            "steamids": settings.steam_id,
            "format": "json",
        },
    )
    response.raise_for_status()
    players = response.json().get("response", {}).get("players", [])
    if not players:
        return None
    return players[0]


def _fetch_recent_games(client: httpx.Client, settings: Settings) -> list[dict]:
    response = client.get(
        "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v0001/",
        params={
            "key": settings.steam_api_key,
            "steamid": settings.steam_id,
            "format": "json",
            "count": 8,
        },
    )
    response.raise_for_status()
    games = response.json().get("response", {}).get("games", [])
    normalized = []
    for game in games:
        normalized.append(
            {
                "appid": game.get("appid"),
                "name": game.get("name") or f"App {game.get('appid')}",
                "playtime_2weeks": game.get("playtime_2weeks", 0),
                "playtime_forever": game.get("playtime_forever", 0),
                "playtime_2weeks_label": _format_minutes_label(game.get("playtime_2weeks", 0)),
                "playtime_forever_label": _format_minutes_label(game.get("playtime_forever", 0)),
                "img_logo_url": game.get("img_logo_url"),
            }
        )
    return normalized


def _fetch_recent_achievements(client: httpx.Client, settings: Settings, recent_games: list[dict]) -> list[dict]:
    achievements: list[dict] = []

    for game in recent_games[:4]:
        appid = game.get("appid")
        if not appid:
            continue

        response = client.get(
            "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/",
            params={
                "key": settings.steam_api_key,
                "steamid": settings.steam_id,
                "appid": appid,
                "l": "portuguese",
            },
        )

        if response.status_code >= 400:
            continue

        body = response.json()
        playerstats = body.get("playerstats", {})
        game_name = playerstats.get("gameName") or game.get("name") or f"App {appid}"

        for item in playerstats.get("achievements", []):
            if int(item.get("achieved", 0)) != 1:
                continue
            unlocktime = int(item.get("unlocktime", 0))
            if unlocktime <= 0:
                continue
            display_name = item.get("name") or item.get("apiname") or "Conquista"
            achievements.append(
                {
                    "game": game_name,
                    "name": display_name,
                    "unlocktime": unlocktime,
                }
            )

    achievements.sort(key=lambda x: x["unlocktime"], reverse=True)
    for achievement in achievements[:6]:
        achievement["unlocktime_label"] = _format_utc_datetime(
            datetime.fromtimestamp(achievement["unlocktime"], timezone.utc)
        )
    return achievements[:6]


def get_steam_recent_games(settings: Settings, count: int = 20) -> list[dict]:
    now = datetime.now(timezone.utc)
    cached_expires_at = _STEAM_RECENT_GAMES_CACHE.get("expires_at")
    if isinstance(cached_expires_at, datetime) and now < cached_expires_at:
        cached = _STEAM_RECENT_GAMES_CACHE.get("data")
        if isinstance(cached, list):
            return cached[:count]

    if not settings.steam_api_key or not settings.steam_id:
        return []

    try:
        with httpx.Client(timeout=8.0) as client:
            recent_games = _fetch_recent_games(client, settings)
        _STEAM_RECENT_GAMES_CACHE["data"] = recent_games
        _STEAM_RECENT_GAMES_CACHE["expires_at"] = now + timedelta(minutes=3)
        return recent_games[:count]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []


def get_steam_dashboard_data(settings: Settings) -> dict:
    now = datetime.now(timezone.utc)
    cached_expires_at = _STEAM_CACHE.get("expires_at")
    if isinstance(cached_expires_at, datetime) and now < cached_expires_at:
        cached = _STEAM_CACHE.get("data")
        if isinstance(cached, dict):
            return cached

    payload = _default_payload()

    if not settings.steam_api_key or not settings.steam_id:
        payload["error"] = "STEAM_API_KEY ou STEAM_ID nao configurado."
        _STEAM_CACHE["data"] = payload
        _STEAM_CACHE["expires_at"] = now + timedelta(seconds=60)
        return payload

    payload["enabled"] = True

    try:
        with httpx.Client(timeout=8.0) as client:
            profile = _fetch_profile(client, settings)
            recent_games = _fetch_recent_games(client, settings)
            recent_achievements = _fetch_recent_achievements(client, settings, recent_games)

        payload["profile"] = profile
        payload["recent_games"] = recent_games
        payload["recent_achievements"] = recent_achievements
        payload["last_updated_label"] = _format_utc_datetime(now)

        _STEAM_CACHE["data"] = payload
        _STEAM_CACHE["expires_at"] = now + timedelta(minutes=3)
        return payload
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        payload["error"] = f"Falha ao buscar dados Steam: {exc}"
        _STEAM_CACHE["data"] = payload
        _STEAM_CACHE["expires_at"] = now + timedelta(seconds=45)
        return payload
