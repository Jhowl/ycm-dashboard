from __future__ import annotations

from app.models import MetadataDraft, VideoAsset
from app.schemas import DraftOut, VideoOut


def latest_active_draft(video: VideoAsset) -> MetadataDraft | None:
    active = [draft for draft in video.drafts if draft.is_active]
    if active:
        return sorted(active, key=lambda d: (d.version, d.created_at), reverse=True)[0]

    if not video.drafts:
        return None

    return sorted(video.drafts, key=lambda d: (d.version, d.created_at), reverse=True)[0]


def video_to_schema(video: VideoAsset) -> VideoOut:
    latest = latest_active_draft(video)
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
