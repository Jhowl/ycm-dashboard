from __future__ import annotations

from typing import Any

from app.models import MetadataDraft, VideoAsset
from app.services.metadata import get_latest_active_draft
from app.schemas import ChapterOut, DraftOut, VideoOut


def _chapters_to_schema(chapters: Any) -> list[ChapterOut]:
    if not chapters:
        return []
    out: list[ChapterOut] = []
    for chap in chapters:
        if not isinstance(chap, dict):
            continue
        try:
            start = int(chap.get("start_seconds") or chap.get("start") or 0)
        except (TypeError, ValueError):
            continue
        title = str(chap.get("title") or "").strip()
        if not title:
            continue
        out.append(ChapterOut(start_seconds=start, title=title))
    return out


def draft_to_schema(draft: MetadataDraft) -> DraftOut:
    return DraftOut(
        id=draft.id,
        video_id=draft.video_id,
        title_ptbr=draft.title_ptbr,
        description_ptbr=draft.description_ptbr,
        tags=list(draft.tags or []),
        thumbnail_path=draft.thumbnail_path,
        thumbnail_prompt=draft.thumbnail_prompt,
        chapters=_chapters_to_schema(draft.chapters or []),
        model_provider=draft.model_provider,
        model_name=draft.model_name,
        language=draft.language,
        version=draft.version,
        is_active=draft.is_active,
        created_at=draft.created_at,
    )


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
        transcript_status=video.transcript_status or "PENDING",
        transcript_language=video.transcript_language,
        transcript_path=video.transcript_path,
        transcript_error=video.transcript_error,
        chapters=_chapters_to_schema(video.chapters or []),
        latest_draft=draft_to_schema(latest) if latest else None,
    )
