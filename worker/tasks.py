from __future__ import annotations

from celery.utils.log import get_task_logger

from app.models import VideoAsset
from app.services.folders import sync_folders_and_videos
from app.services.metadata import generate_metadata_draft, upload_video
from app.services.thumbnail_lab import ensure_thumbnail_lab_assets
from worker.celery_app import celery_app
from worker.runtime import worker_session

logger = get_task_logger(__name__)


@celery_app.task(name="tasks.scan_folders")
def scan_folders_task() -> dict:
    with worker_session() as (settings, db):
        result = sync_folders_and_videos(db, settings)

    logger.info("Scan finished: %s", result)
    return result


@celery_app.task(name="tasks.generate_metadata")
def generate_metadata_task(video_id: str) -> dict:
    with worker_session() as (settings, db):
        draft = generate_metadata_draft(db, settings, video_id)

    logger.info("Draft generated for video=%s draft=%s", video_id, draft.id)
    return {"video_id": video_id, "draft_id": draft.id}


@celery_app.task(name="tasks.generate_thumbnail_options")
def generate_thumbnail_options_task(video_id: str) -> dict:
    with worker_session() as (_, db):
        video = db.get(VideoAsset, video_id)
        if not video:
            raise ValueError("Video not found")
        files = ensure_thumbnail_lab_assets(video, force_regen=True)

    logger.info("Thumbnail options generated for video=%s", video_id)
    return {"video_id": video_id, "count": len(files)}


@celery_app.task(name="tasks.upload_video")
def upload_video_task(video_id: str) -> dict:
    with worker_session() as (settings, db):
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
