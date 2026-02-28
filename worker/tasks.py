from __future__ import annotations

from celery.utils.log import get_task_logger

from app.config import get_settings
from app.db import create_engine_and_session_factory, init_db
from app.models import VideoAsset
from app.services.folders import sync_folders_and_videos
from app.services.metadata import generate_metadata_draft, upload_video
from worker.celery_app import celery_app

logger = get_task_logger(__name__)


@celery_app.task(name="tasks.scan_folders")
def scan_folders_task() -> dict:
    settings = get_settings()
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    init_db(engine)

    with session_factory() as db:
        result = sync_folders_and_videos(db, settings)

    logger.info("Scan finished: %s", result)
    return result


@celery_app.task(name="tasks.generate_metadata")
def generate_metadata_task(video_id: str) -> dict:
    settings = get_settings()
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    init_db(engine)

    with session_factory() as db:
        draft = generate_metadata_draft(db, settings, video_id)

    logger.info("Draft generated for video=%s draft=%s", video_id, draft.id)
    return {"video_id": video_id, "draft_id": draft.id}


@celery_app.task(name="tasks.upload_video")
def upload_video_task(video_id: str) -> dict:
    settings = get_settings()
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    init_db(engine)

    with session_factory() as db:
        video = db.get(VideoAsset, video_id)
        if not video:
            raise ValueError("Video not found")

        try:
            uploaded = upload_video(db, settings, video_id)
            refreshed = db.get(VideoAsset, video_id)
            if refreshed:
                payload = dict(refreshed.session_payload or {})
                payload["upload_task_status"] = "SUCCESS"
                refreshed.session_payload = payload
                db.commit()

            logger.info("Upload finished for video=%s", video_id)
            return {"video_id": video_id, "uploaded_url": uploaded.uploaded_url}
        except Exception as exc:
            failed = db.get(VideoAsset, video_id)
            if failed:
                payload = dict(failed.session_payload or {})
                payload["upload_task_status"] = "FAILURE"
                payload["upload_task_error"] = str(exc)[:500]
                failed.session_payload = payload
                db.commit()
            raise
