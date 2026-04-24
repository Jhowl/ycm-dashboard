"""Lightweight JSON cache backed by Redis when available, dict fallback otherwise."""
from __future__ import annotations

import json
import time
from typing import Any

from app.config import Settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

_LOCAL_CACHE: dict[str, tuple[float, str]] = {}
_REDIS_CLIENT = None
_REDIS_TRIED = False


def _redis(settings: Settings):
    global _REDIS_CLIENT, _REDIS_TRIED
    if _REDIS_CLIENT is not None or _REDIS_TRIED:
        return _REDIS_CLIENT
    _REDIS_TRIED = True
    try:
        import redis  # type: ignore

        _REDIS_CLIENT = redis.Redis.from_url(
            settings.redis_url, socket_timeout=1.5, socket_connect_timeout=1.5
        )
        _REDIS_CLIENT.ping()
        logger.info("cache_redis_connected", extra={"url": settings.redis_url})
    except Exception as exc:  # noqa: BLE001
        logger.info("cache_redis_unavailable", extra={"err": str(exc)[:200]})
        _REDIS_CLIENT = None
    return _REDIS_CLIENT


def cache_get(settings: Settings, key: str) -> Any | None:
    client = _redis(settings)
    if client is not None:
        try:
            raw = client.get(f"ycm:{key}")
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_get_redis_failed", extra={"err": str(exc)[:120]})

    entry = _LOCAL_CACHE.get(key)
    if not entry:
        return None
    expires_at, raw = entry
    if expires_at < time.time():
        _LOCAL_CACHE.pop(key, None)
        return None
    return json.loads(raw)


def cache_set(settings: Settings, key: str, value: Any, ttl_seconds: int) -> None:
    raw = json.dumps(value, ensure_ascii=False)
    client = _redis(settings)
    if client is not None:
        try:
            client.setex(f"ycm:{key}", max(1, int(ttl_seconds)), raw)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_set_redis_failed", extra={"err": str(exc)[:120]})

    _LOCAL_CACHE[key] = (time.time() + max(1, int(ttl_seconds)), raw)


def cache_invalidate(settings: Settings, key: str) -> None:
    client = _redis(settings)
    if client is not None:
        try:
            client.delete(f"ycm:{key}")
        except Exception:  # noqa: BLE001
            pass
    _LOCAL_CACHE.pop(key, None)


def reset_local_cache_for_tests() -> None:
    _LOCAL_CACHE.clear()
