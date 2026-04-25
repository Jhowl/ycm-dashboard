"""Celery jobs introspection — used by the /jobs page and APIs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _format_job(meta: dict[str, Any], state: str, worker: str | None = None) -> dict[str, Any]:
    received = meta.get("time_start") or meta.get("eta")
    received_label: str | None = None
    if isinstance(received, (int, float)):
        try:
            received_label = datetime.fromtimestamp(float(received), tz=timezone.utc).isoformat()
        except (OSError, ValueError):
            received_label = None
    elif isinstance(received, str):
        received_label = received
    return {
        "id": str(meta.get("id") or meta.get("request", {}).get("id", "")),
        "name": str(meta.get("name") or meta.get("type") or meta.get("request", {}).get("type", "")),
        "state": state,
        "received_at": received_label,
        "args": list(meta.get("args") or []),
        "worker": worker,
    }


def collect_jobs(celery_app: Any, *, timeout: float = 1.0) -> dict[str, Any]:
    """Return a dict matching JobsOut schema. Never raises."""
    try:
        inspector = celery_app.control.inspect(timeout=timeout)
    except Exception as exc:
        logger.warning("celery_inspect_init_failed: %s", exc)
        return {"active": [], "reserved": [], "scheduled": [], "workers_online": 0, "error": str(exc)}

    out: dict[str, list[dict[str, Any]]] = {"active": [], "reserved": [], "scheduled": []}
    try:
        for kind in ("active", "reserved", "scheduled"):
            method = getattr(inspector, kind)
            data = method() or {}
            for worker, jobs in data.items():
                for job in jobs or []:
                    out[kind].append(_format_job(job, kind.upper(), worker=worker))
        ping = inspector.ping() or {}
        return {**out, "workers_online": len(ping), "error": None}
    except Exception as exc:
        logger.warning("celery_inspect_failed: %s", exc)
        return {**out, "workers_online": 0, "error": str(exc)}
