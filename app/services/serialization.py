from __future__ import annotations

from app.models import VideoAsset
from app.services.metadata import get_latest_active_draft
from app.schemas import DraftOut, VideoOut


def video_to_schema(video: VideoAsset) -> VideoOut:
    latest = get_latest_active_draft(video)
    return VideoOut(
        id=video.id,
        folder_id=video.folder_id,
        filename=video.filename,
        source_path=video.source_path,
        recorded_at=video.recorded_at,
        duration_sec=video.duration_sec,
        series_number=video.series_number,
        thumbnail_prompt=video.thumbnail_prompt,
        status=video.status,
        language=video.language,
        uploaded_url=video.uploaded_url,
        created_at=video.created_at,
        latest_draft=DraftOut.model_validate(latest) if latest else None,
    )
