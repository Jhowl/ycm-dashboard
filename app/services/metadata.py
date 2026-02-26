from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ChannelDefaults, MetadataDraft, SeriesFolder, VideoAsset, VideoStatus


def get_latest_draft(video: VideoAsset) -> MetadataDraft | None:
    if not video.drafts:
        return None
    ordered = sorted(video.drafts, key=lambda d: (d.version, d.created_at), reverse=True)
    return ordered[0]


def _next_draft_version(video: VideoAsset) -> int:
    latest = get_latest_draft(video)
    if not latest:
        return 1
    return latest.version + 1


def _extract_episode_number(db: Session, video: VideoAsset) -> int:
    videos = db.execute(
        select(VideoAsset)
        .where(VideoAsset.folder_id == video.folder_id)
        .order_by(VideoAsset.recorded_at.is_(None), VideoAsset.recorded_at.asc(), VideoAsset.created_at.asc())
    ).scalars()

    for index, item in enumerate(videos, start=1):
        if item.id == video.id:
            return index

    return 1


def _resolve_episode_number(db: Session, video: VideoAsset) -> int:
    if video.series_number and video.series_number > 0:
        return video.series_number
    return _extract_episode_number(db, video)


def _build_tags(folder: SeriesFolder, defaults: ChannelDefaults) -> list[str]:
    tags = [folder.name.lower(), "gameplay", "sem comentarios", "pt-br"]
    tags.extend(defaults.default_tags)

    normalized: list[str] = []
    seen = set()
    for tag in tags:
        clean = " ".join(tag.split()).strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(clean)

    return normalized[:15]


def _build_description(
    folder: SeriesFolder,
    video: VideoAsset,
    defaults: ChannelDefaults,
    episode_number: int,
) -> str:
    payload = video.session_payload or {}
    achievements = payload.get("achievements_unlocked") or []
    playtime = payload.get("playtime_minutes")

    duration = f"{video.duration_sec // 60} minutos" if video.duration_sec else "duracao indisponivel"

    lines = [
        f"Serie: {folder.name}",
        f"Episodio: {episode_number:02d}",
        f"Idioma: {defaults.language}",
        f"Formato: gameplay sem comentarios",
        f"Duracao aproximada: {duration}",
    ]

    if playtime is not None:
        lines.append(f"Playtime Steam no periodo: {playtime} minutos")

    if achievements:
        lines.append("Conquistas desbloqueadas: " + ", ".join(str(a) for a in achievements))
    else:
        lines.append("Conquistas desbloqueadas: sem novas conquistas registradas")

    if video.thumbnail_prompt:
        lines.append(f"Prompt thumbnail: {video.thumbnail_prompt}")

    lines.append(f"PC: {defaults.pc_config}")
    lines.append("")
    lines.append(defaults.default_description_block)

    return "\n".join(lines)


def _escape_drawtext_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    escaped = escaped.replace("%", "\\%")
    escaped = escaped.replace("\n", " ")
    return escaped


def _generate_thumbnail(
    video_path: Path,
    output_path: Path,
    episode_number: int,
    thumbnail_prompt: str | None,
) -> str | None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base_path = output_path.with_name(f"{output_path.stem}_base{output_path.suffix}")

    command = [
        "ffmpeg",
        "-y",
        "-ss",
        "00:00:05",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        str(base_path),
    ]

    try:
        subprocess.run(command, capture_output=True, check=True, timeout=30)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None

    headline = f"EP {episode_number:02d}"
    prompt_text = (thumbnail_prompt or "").strip()
    if prompt_text:
        prompt_text = prompt_text[:70]

    draw_filters = [
        "drawbox=x=0:y=ih-160:w=iw:h=160:color=black@0.58:t=fill",
        (
            "drawtext=text='{}':x=30:y=ih-132:fontsize=56:fontcolor=white:"
            "box=0:shadowcolor=black@0.8:shadowx=2:shadowy=2"
        ).format(_escape_drawtext_value(headline)),
    ]
    if prompt_text:
        draw_filters.append(
            (
                "drawtext=text='{}':x=30:y=ih-66:fontsize=34:fontcolor=white:"
                "box=0:shadowcolor=black@0.8:shadowx=2:shadowy=2"
            ).format(_escape_drawtext_value(prompt_text))
        )

    overlay_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(base_path),
        "-vf",
        ",".join(draw_filters),
        str(output_path),
    ]

    try:
        subprocess.run(overlay_command, capture_output=True, check=True, timeout=30)
        return str(output_path)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        try:
            shutil.copyfile(base_path, output_path)
            return str(output_path)
        except OSError:
            return str(base_path)
    finally:
        if base_path.exists():
            base_path.unlink(missing_ok=True)


def generate_metadata_draft(db: Session, settings: Settings, video_id: str) -> MetadataDraft:
    video = db.get(VideoAsset, video_id)
    if not video:
        raise ValueError("Video not found")

    folder = db.get(SeriesFolder, video.folder_id)
    if not folder:
        raise ValueError("Folder not found")

    defaults = db.get(ChannelDefaults, 1)
    if not defaults:
        defaults = ChannelDefaults(id=1)
        db.add(defaults)
        db.flush()

    episode_number = _resolve_episode_number(db, video)
    title = f"{folder.name} Gameplay PT-BR | Episodio {episode_number:02d}"
    description = _build_description(folder, video, defaults, episode_number)
    tags = _build_tags(folder, defaults)

    for draft in video.drafts:
        draft.is_active = False

    thumbnail_path = _generate_thumbnail(
        Path(video.source_path),
        Path(settings.artifacts_root) / "thumbnails" / f"{video.id}.jpg",
        episode_number,
        video.thumbnail_prompt,
    )

    draft = MetadataDraft(
        video_id=video.id,
        title_ptbr=title,
        description_ptbr=description,
        tags=tags,
        thumbnail_path=thumbnail_path,
        model_provider=settings.default_model_provider,
        language=defaults.language,
        version=_next_draft_version(video),
        is_active=True,
    )

    video.status = VideoStatus.DRAFT_READY.value
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def update_video_settings(
    db: Session,
    video_id: str,
    series_number: int | None,
    thumbnail_prompt: str | None,
) -> VideoAsset:
    video = db.get(VideoAsset, video_id)
    if not video:
        raise ValueError("Video not found")

    if series_number is not None and series_number < 1:
        raise ValueError("series_number must be greater than zero")

    video.series_number = series_number
    cleaned_prompt = (thumbnail_prompt or "").strip()
    video.thumbnail_prompt = cleaned_prompt or None
    db.commit()
    db.refresh(video)
    return video


def approve_video(db: Session, video_id: str) -> VideoAsset:
    video = db.get(VideoAsset, video_id)
    if not video:
        raise ValueError("Video not found")

    video.status = VideoStatus.APPROVED.value
    db.commit()
    db.refresh(video)
    return video


def reject_video(db: Session, video_id: str) -> VideoAsset:
    video = db.get(VideoAsset, video_id)
    if not video:
        raise ValueError("Video not found")

    video.status = VideoStatus.INGESTED.value
    db.commit()
    db.refresh(video)
    return video


def upload_video(db: Session, video_id: str) -> VideoAsset:
    video = db.get(VideoAsset, video_id)
    if not video:
        raise ValueError("Video not found")

    if video.status != VideoStatus.APPROVED.value:
        raise PermissionError("Video must be approved before upload")

    video.status = VideoStatus.UPLOADED.value
    video.uploaded_url = f"https://youtube.com/watch?v=mock-{video.id[:8]}"
    db.commit()
    db.refresh(video)
    return video
