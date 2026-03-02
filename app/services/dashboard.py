from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import SeriesFolder, VideoAsset
from app.time_utils import format_datetime_ny


def get_youtube_token_status(token_file: str) -> tuple[bool, str | None]:
    token_path = Path(token_file)
    if not token_path.exists():
        return False, None

    updated_at = format_datetime_ny(datetime.fromtimestamp(token_path.stat().st_mtime, timezone.utc))
    return True, updated_at


def build_home_stats(db: Session) -> dict[str, int]:
    return {
        "folders_total": db.execute(select(func.count(SeriesFolder.id))).scalar_one(),
        "folders_active": db.execute(
            select(func.count(SeriesFolder.id)).where(SeriesFolder.active.is_(True))
        ).scalar_one(),
        "pending_drafts": db.execute(
            select(func.count(VideoAsset.id)).where(VideoAsset.status.in_(["INGESTED", "DRAFT_READY"]))
        ).scalar_one(),
        "ready_to_upload": db.execute(
            select(func.count(VideoAsset.id)).where(VideoAsset.status == "APPROVED")
        ).scalar_one(),
    }
