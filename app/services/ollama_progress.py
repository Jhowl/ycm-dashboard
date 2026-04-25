"""Per-video Ollama generation progress tracking.

States: IDLE, PENDING, RUNNING, READY, FAILED.

The service updates ``VideoAsset.ollama_status``, ``ollama_progress`` (a JSON
map of step -> status), and ``ollama_error`` so the UI can show a live chip.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import VideoAsset

logger = logging.getLogger(__name__)


STEPS = ("title", "description", "tags", "thumbnail_prompt", "chapters")


def reset(db: Session, video_id: str, steps: Iterable[str] = STEPS) -> None:
    video = db.get(VideoAsset, video_id)
    if not video:
        return
    video.ollama_status = "PENDING"
    video.ollama_progress = {step: "PENDING" for step in steps}
    video.ollama_error = None
    db.commit()


def mark_running(db: Session, video_id: str, step: str) -> None:
    video = db.get(VideoAsset, video_id)
    if not video:
        return
    video.ollama_status = "RUNNING"
    progress = dict(video.ollama_progress or {})
    progress[step] = "RUNNING"
    video.ollama_progress = progress
    db.commit()


def mark_done(db: Session, video_id: str, step: str) -> None:
    video = db.get(VideoAsset, video_id)
    if not video:
        return
    progress = dict(video.ollama_progress or {})
    progress[step] = "DONE"
    video.ollama_progress = progress
    if all(progress.get(s) == "DONE" for s in STEPS):
        video.ollama_status = "READY"
    db.commit()


def mark_skipped(db: Session, video_id: str, step: str) -> None:
    video = db.get(VideoAsset, video_id)
    if not video:
        return
    progress = dict(video.ollama_progress or {})
    progress[step] = "SKIPPED"
    video.ollama_progress = progress
    db.commit()


def mark_failed(db: Session, video_id: str, step: str, err: str) -> None:
    video = db.get(VideoAsset, video_id)
    if not video:
        return
    video.ollama_status = "FAILED"
    progress = dict(video.ollama_progress or {})
    progress[step] = "FAILED"
    video.ollama_progress = progress
    video.ollama_error = (err or "")[:500]
    db.commit()
