"""Steam Web API integration with Redis-backed caching."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings
from app.logging_setup import get_logger
from app.services.cache import cache_get, cache_set
from app.time_utils import format_datetime_ny

logger = get_logger(__name__)

_ENDPOINT_PROFILE = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
_ENDPOINT_RECENT = "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v0001/"
_ENDPOINT_OWNED = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
_ENDPOINT_ACH = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
_ENDPOINT_SCHEMA = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"


@dataclass(slots=True)
class SteamConfigError(RuntimeError):
    message: str

    def __str__(self) -> str:
        return self.message


def _format_minutes_label(minutes: int) -> str:
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes}m"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h" if remainder == 0 else f"{hours}h {remainder}m"


def _normalize_game(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "appid": raw.get("appid"),
        "name": raw.get("name") or f"App {raw.get('appid')}",
        "playtime_2weeks": int(raw.get("playtime_2weeks", 0) or 0),
        "playtime_forever": int(raw.get("playtime_forever", 0) or 0),
        "playtime_2weeks_label": _format_minutes_label(int(raw.get("playtime_2weeks", 0) or 0)),
        "playtime_forever_label": _format_minutes_label(int(raw.get("playtime_forever", 0) or 0)),
        "img_logo_url": raw.get("img_logo_url"),
        "img_icon_url": raw.get("img_icon_url"),
    }


def _default_payload() -> dict[str, Any]:
    return {
        "enabled": False,
        "error": None,
        "profile": None,
        "recent_games": [],
        "recent_achievements": [],
        "last_updated_label": None,
    }


# ---- public API --------------------------------------------------------
def get_steam_recent_games(settings: Settings, count: int = 20) -> list[dict[str, Any]]:
    key = "steam:recent_games"
    cached = cache_get(settings, key)
    if isinstance(cached, list):
        return cached[:count]

    if not settings.steam_api_key or not settings.steam_id:
        cache_set(settings, key, [], ttl_seconds=60)
        return []

    try:
        with httpx.Client(timeout=8.0) as client:
            games = _fetch_recent_games(client, settings)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("steam_recent_games_failed", extra={"err": str(exc)[:200]})
        return []

    cache_set(settings, key, games, ttl_seconds=settings.steam_cache_ttl_seconds)
    return games[:count]


def get_steam_owned_games(settings: Settings) -> list[dict[str, Any]]:
    key = "steam:owned_games"
    cached = cache_get(settings, key)
    if isinstance(cached, list):
        return cached

    if not settings.steam_api_key or not settings.steam_id:
        return []

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                _ENDPOINT_OWNED,
                params={
                    "key": settings.steam_api_key,
                    "steamid": settings.steam_id,
                    "include_appinfo": 1,
                    "include_played_free_games": 1,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            games = [_normalize_game(g) for g in resp.json().get("response", {}).get("games", [])]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("steam_owned_games_failed", extra={"err": str(exc)[:200]})
        return []

    cache_set(settings, key, games, ttl_seconds=max(600, settings.steam_cache_ttl_seconds * 3))
    return games


def get_steam_dashboard_data(settings: Settings) -> dict[str, Any]:
    key = "steam:dashboard"
    cached = cache_get(settings, key)
    if isinstance(cached, dict):
        return cached

    payload = _default_payload()
    if not settings.steam_api_key or not settings.steam_id:
        payload["error"] = "STEAM_API_KEY ou STEAM_ID nao configurado."
        cache_set(settings, key, payload, ttl_seconds=60)
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
        payload["last_updated_label"] = format_datetime_ny(datetime.now(timezone.utc))
        cache_set(settings, key, payload, ttl_seconds=settings.steam_cache_ttl_seconds)
        return payload
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        payload["error"] = f"Falha ao buscar dados Steam: {exc}"
        cache_set(settings, key, payload, ttl_seconds=45)
        logger.warning("steam_dashboard_failed", extra={"err": str(exc)[:200]})
        return payload


def get_achievements_for_window(
    settings: Settings,
    steam_app_id: int,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    """Return unlocked achievements for one Steam app inside [start_utc, end_utc]."""
    if not settings.steam_api_key or not settings.steam_id or not steam_app_id:
        return []

    start_utc, end_utc = _order_window(start_utc, end_utc)
    start_ts, end_ts = int(start_utc.timestamp()), int(end_utc.timestamp())

    try:
        with httpx.Client(timeout=10.0) as client:
            body = _fetch_achievements_raw(client, settings, steam_app_id)
            schema = _fetch_schema_for_game(client, settings, steam_app_id)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("steam_achievements_failed", extra={"err": str(exc)[:200], "app_id": steam_app_id})
        return []

    schema_by_name = {item.get("name"): item for item in schema}
    playerstats = body.get("playerstats", {})
    game_name = playerstats.get("gameName") or f"App {steam_app_id}"

    selected: list[dict[str, Any]] = []
    for item in playerstats.get("achievements", []):
        if int(item.get("achieved", 0)) != 1:
            continue
        unlock_ts = int(item.get("unlocktime", 0) or 0)
        if unlock_ts <= 0 or unlock_ts < start_ts or unlock_ts > end_ts:
            continue

        schema_item = schema_by_name.get(item.get("apiname")) or {}
        selected.append(
            {
                "name": str(item.get("name") or schema_item.get("displayName") or item.get("apiname") or "Conquista"),
                "api_name": item.get("apiname"),
                "description": schema_item.get("description"),
                "icon": schema_item.get("icon"),
                "unlocktime": unlock_ts,
                "unlocktime_label": format_datetime_ny(datetime.fromtimestamp(unlock_ts, timezone.utc)),
                "game": game_name,
                "appid": int(steam_app_id),
            }
        )

    selected.sort(key=lambda x: x["unlocktime"])
    return selected


def auto_match_folder_by_playtime(
    folder_name: str,
    *,
    recent_games: list[dict[str, Any]] | None = None,
    owned_games: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Pick the best Steam game candidate for a folder.

    Priority:
      1. Exact normalized name match in recently-played list (high playtime).
      2. Substring match in recently-played.
      3. Exact match in owned-games list.
      4. Substring match in owned-games list.
    """
    folder_norm = _normalize_for_match(folder_name)
    if not folder_norm:
        return None

    def best_in(pool: list[dict[str, Any]], *, exact: bool) -> dict[str, Any] | None:
        scored: list[tuple[int, dict[str, Any]]] = []
        for game in pool:
            game_norm = _normalize_for_match(str(game.get("name") or ""))
            if not game_norm:
                continue
            if exact:
                if game_norm == folder_norm:
                    scored.append((int(game.get("playtime_2weeks") or 0), game))
            elif folder_norm in game_norm or game_norm in folder_norm:
                scored.append((int(game.get("playtime_2weeks") or 0), game))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1]

    for pool in (recent_games or [], owned_games or []):
        if not pool:
            continue
        match = best_in(pool, exact=True)
        if match:
            return match

    for pool in (recent_games or [], owned_games or []):
        if not pool:
            continue
        match = best_in(pool, exact=False)
        if match:
            return match

    return None


# ---- internal ----------------------------------------------------------
def _fetch_profile(client: httpx.Client, settings: Settings) -> dict | None:
    resp = client.get(
        _ENDPOINT_PROFILE,
        params={"key": settings.steam_api_key, "steamids": settings.steam_id, "format": "json"},
    )
    resp.raise_for_status()
    players = resp.json().get("response", {}).get("players", [])
    return players[0] if players else None


def _fetch_recent_games(client: httpx.Client, settings: Settings) -> list[dict[str, Any]]:
    resp = client.get(
        _ENDPOINT_RECENT,
        params={
            "key": settings.steam_api_key,
            "steamid": settings.steam_id,
            "format": "json",
            "count": 16,
        },
    )
    resp.raise_for_status()
    games = resp.json().get("response", {}).get("games", []) or []
    return [_normalize_game(g) for g in games]


def _fetch_achievements_raw(client: httpx.Client, settings: Settings, app_id: int) -> dict[str, Any]:
    resp = client.get(
        _ENDPOINT_ACH,
        params={
            "key": settings.steam_api_key,
            "steamid": settings.steam_id,
            "appid": app_id,
            "l": "portuguese",
        },
    )
    if resp.status_code >= 400:
        return {}
    return resp.json() or {}


def _fetch_schema_for_game(
    client: httpx.Client, settings: Settings, app_id: int
) -> list[dict[str, Any]]:
    resp = client.get(
        _ENDPOINT_SCHEMA,
        params={"key": settings.steam_api_key, "appid": app_id, "l": "portuguese"},
    )
    if resp.status_code >= 400:
        return []
    try:
        body = resp.json() or {}
    except ValueError:
        return []
    return body.get("game", {}).get("availableGameStats", {}).get("achievements", []) or []


def _fetch_recent_achievements(
    client: httpx.Client, settings: Settings, recent_games: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for game in recent_games[:4]:
        app_id = game.get("appid")
        if not app_id:
            continue
        try:
            body = _fetch_achievements_raw(client, settings, int(app_id))
        except (httpx.HTTPError, ValueError):
            continue
        playerstats = body.get("playerstats", {})
        game_name = playerstats.get("gameName") or game.get("name") or f"App {app_id}"
        for item in playerstats.get("achievements", []) or []:
            if int(item.get("achieved", 0)) != 1:
                continue
            unlock_ts = int(item.get("unlocktime", 0) or 0)
            if unlock_ts <= 0:
                continue
            out.append(
                {
                    "game": game_name,
                    "name": item.get("name") or item.get("apiname") or "Conquista",
                    "unlocktime": unlock_ts,
                    "unlocktime_label": format_datetime_ny(
                        datetime.fromtimestamp(unlock_ts, timezone.utc)
                    ),
                }
            )

    out.sort(key=lambda x: x["unlocktime"], reverse=True)
    return out[:6]


def _order_window(start_utc: datetime, end_utc: datetime) -> tuple[datetime, datetime]:
    start = start_utc if start_utc.tzinfo else start_utc.replace(tzinfo=timezone.utc)
    end = end_utc if end_utc.tzinfo else end_utc.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end < start:
        start, end = end, start
    return start, end


def _normalize_for_match(value: str) -> str:
    from slugify import slugify

    return slugify(value or "", lowercase=True).replace("-", "")
