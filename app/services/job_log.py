"""Persistent log of Celery task runs — survives restarts and is the source
of truth for the /jobs page (history + errors).

Celery's own ``inspect()`` only knows about *currently active* tasks; once a
task finishes (or fails), it disappears from that view. We record each run
into the ``job_runs`` table via Celery signals so the UI can show history.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobRun

logger = logging.getLogger(__name__)


def record_run(
    db: Session,
    *,
    task_id: str,
    name: str | None = None,
    args: list[Any] | None = None,
    status: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    result: Any = None,
    error: str | None = None,
) -> None:
    """Upsert a JobRun row by celery task id."""
    if not task_id:
        return

    run = db.get(JobRun, task_id)
    created = run is None
    if created:
        run = JobRun(id=task_id, name=name or "unknown")
        db.add(run)

    if name and (run.name in (None, "", "unknown")):
        run.name = name

    if args is not None and not run.args_json:
        try:
            run.args_json = json.dumps(args, ensure_ascii=False, default=str)[:2000]
        except (TypeError, ValueError):
            run.args_json = str(args)[:2000]

    if status:
        run.status = status

    if started_at is not None and run.started_at is None:
        run.started_at = started_at

    if finished_at is not None:
        run.finished_at = finished_at

    if result is not None and not run.result:
        try:
            run.result = json.dumps(result, ensure_ascii=False, default=str)[:2000]
        except (TypeError, ValueError):
            run.result = str(result)[:2000]

    if error is not None:
        run.error = error[:4000]

    db.commit()


def recent_runs(db: Session, limit: int = 50) -> list[JobRun]:
    return list(
        db.execute(
            select(JobRun).order_by(JobRun.created_at.desc()).limit(limit)
        ).scalars()
    )
