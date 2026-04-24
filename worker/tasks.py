from __future__ import annotations

from celery.utils.log import get_task_logger

from app.models import VideoAsset
from app.services.folders import sync_folders_and_videos
from app.services.metadata import generate_metadata_draft, upload_video
from app.services.thumbnail_lab import ensure_thumbnail_lab_assets
from app.services.transcription import transcribe_video
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


@celery_app.task(
    name="tasks.transcribe_video",
    bind=True,
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def transcribe_video_task(self, video_id: str) -> dict:
    """Run Whisper on a video in the background.

    Optionally chains metadata generation after a successful transcript
    if ``whisper_auto_run`` is on.
    """
    with worker_session() as (settings, db):
        result = transcribe_video(db, settings, video_id)
        auto_generate_draft = bool(settings.ollama_enabled and settings.whisper_auto_run)

    logger.info(
        "Transcription finished for video=%s language=%s segments=%d",
        video_id,
        result.language,
        len(result.segments),
    )

    if auto_generate_draft:
        try:
            generate_metadata_task.delay(video_id)
        except Exception as exc:
            logger.warning("Failed to chain metadata task for video=%s err=%s", video_id, exc)

    return {
        "video_id": video_id,
        "language": result.language,
        "segments": len(result.segments),
        "transcript_path": result.transcript_path,
    }


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
