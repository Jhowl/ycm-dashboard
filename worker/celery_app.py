from __future__ import annotations

from datetime import datetime, timezone

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "ycm_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

celery_app.autodiscover_tasks(["worker"])


# ---------------------------------------------------------------------
# Persistent job log — every worker-side run upserts a row in job_runs.
# ---------------------------------------------------------------------
_session_factory = None


def _ensure_session_factory():
    global _session_factory
    if _session_factory is None:
        from app.db import create_engine_and_session_factory, init_db

        engine, factory = create_engine_and_session_factory(settings.database_url)
        init_db(engine)
        _session_factory = factory
    return _session_factory


def _safe_record(**kwargs):
    try:
        from app.services.job_log import record_run

        factory = _ensure_session_factory()
        with factory() as db:
            record_run(db, **kwargs)
    except Exception:  # noqa: BLE001 — never let logging break the worker
        pass


@task_prerun.connect
def _on_task_prerun(task_id=None, task=None, args=None, **_kwargs):
    name = getattr(task, "name", None)
    _safe_record(
        task_id=str(task_id) if task_id else "",
        name=name,
        args=list(args or []),
        status="STARTED",
        started_at=datetime.now(timezone.utc),
    )


@task_postrun.connect
def _on_task_postrun(task_id=None, task=None, retval=None, state=None, **_kwargs):
    final = "SUCCESS" if (state or "").upper() == "SUCCESS" else (state or "FINISHED")
    payload = retval if isinstance(retval, (dict, list, str, int, float, bool)) else str(retval)
    _safe_record(
        task_id=str(task_id) if task_id else "",
        name=getattr(task, "name", None),
        status=final,
        finished_at=datetime.now(timezone.utc),
        result=payload,
    )


@task_failure.connect
def _on_task_failure(task_id=None, exception=None, einfo=None, **_kwargs):
    err = ""
    if einfo is not None:
        err = str(einfo)
    if not err and exception is not None:
        err = f"{type(exception).__name__}: {exception}"
    _safe_record(
        task_id=str(task_id) if task_id else "",
        status="FAILURE",
        finished_at=datetime.now(timezone.utc),
        error=err or "unknown",
    )
