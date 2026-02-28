from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ChannelDefaults, MetadataDraft, SeriesFolder, VideoAsset, VideoStatus
from app.services.game_defaults import get_game_tag_defaults
from app.services.steam import get_achievements_for_window
from app.services.youtube_publish import upload_video_to_youtube


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

    per_game = get_game_tag_defaults()
    folder_key = folder.name.strip().lower()
    matched_tags: list[str] = []
    for game_name, game_tags in per_game.items():
        game_key = game_name.strip().lower()
        if not game_key:
            continue
        if game_key == folder_key or game_key in folder_key or folder_key in game_key:
            matched_tags.extend(game_tags)

    tags.extend(matched_tags)

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

    duration_minutes = (video.duration_sec // 60) if video.duration_sec else None
    duration = f"{duration_minutes} minutos" if duration_minutes is not None else "duracao indisponivel"

    folder_name = (folder.name or "").lower()
    is_requiem = "resident evil" in folder_name or "requiem" in folder_name

    if is_requiem:
        lines = [
            f"No episódio {episode_number:02d} de Resident Evil Requiem (Resident Evil 9), acompanhe gameplay sem comentários em PT-BR com foco na progressão da campanha, exploração, combate e clima de survival horror.",
            "",
            "🎮 Conteúdo deste vídeo:",
            "- Gameplay sem comentários (no commentary)",
            "- Trechos com Claire e Leon",
            "- Exploração + combate + progressão de história",
            "- Conquistas desbloqueadas durante a gameplay",
            "",
            f"🧟 Jogo: {folder.name}",
            f"🌍 Idioma: {defaults.language}",
            f"🖥️ Plataforma: PC ({defaults.pc_config})",
            f"⏱️ Duração: ~{duration_minutes} min" if duration_minutes is not None else "⏱️ Duração: indisponível",
        ]
        if playtime is not None:
            lines.append(f"🕹️ Playtime Steam no período: {playtime} minutos")
        if achievements:
            lines.append("🏆 Conquistas desbloqueadas: " + ", ".join(str(a) for a in achievements))
        else:
            lines.append("🏆 Conquistas desbloqueadas: sem novas conquistas registradas")

        lines.extend([
            "",
            "Se curtir, deixa o like e se inscreve para acompanhar a série completa.",
            "Canal: https://www.youtube.com/@aggresiveHamster",
            "",
            "#ResidentEvilRequiem #ResidentEvil9 #GameplayPTBR #SemComentarios #SurvivalHorror",
        ])
        return "\n".join(lines)

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

    # Auto-correlate Steam achievements by video time window when possible.
    if folder.steam_app_id and video.recorded_at and video.duration_sec:
        start_utc = video.recorded_at
        end_utc = video.recorded_at + timedelta(seconds=max(1, int(video.duration_sec)))
        matched = get_achievements_for_window(settings, int(folder.steam_app_id), start_utc, end_utc)

        payload = dict(video.session_payload or {})
        if matched:
            payload["achievements_unlocked"] = [item.get("name") for item in matched if item.get("name")]
            payload["achievements_unlocked_detailed"] = matched
        else:
            payload["achievements_unlocked"] = []
            payload["achievements_unlocked_detailed"] = []
        video.session_payload = payload

    episode_number = _resolve_episode_number(db, video)
    duration_minutes = (video.duration_sec // 60) if video.duration_sec else None
    folder_name = (folder.name or "").lower()
    if "resident evil" in folder_name or "requiem" in folder_name:
        if duration_minutes is not None:
            title = f"Resident Evil Requiem #{episode_number:02d} | {duration_minutes} MIN de Gameplay (PT-BR, Sem Comentários)"
        else:
            title = f"Resident Evil Requiem #{episode_number:02d} | Gameplay PT-BR (Sem Comentários)"
    else:
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
    if cleaned_prompt.lower() in {"undefined", "null", "none"}:
        cleaned_prompt = ""
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


def upload_video(db: Session, settings: Settings, video_id: str) -> VideoAsset:
    video = db.get(VideoAsset, video_id)
    if not video:
        raise ValueError("Video not found")

    latest_draft = get_latest_draft(video)
    if not latest_draft:
        # Auto-generate draft so upload can be one-click.
        latest_draft = generate_metadata_draft(db, settings, video_id)

    defaults = db.get(ChannelDefaults, 1)
    visibility = defaults.default_visibility if defaults else "private"

    if settings.dry_run:
        video.status = VideoStatus.UPLOADED.value
        video.uploaded_url = f"https://youtube.com/watch?v=mock-{video.id[:8]}"
    else:
        youtube_url = upload_video_to_youtube(
            settings,
            title=latest_draft.title_ptbr,
            description=latest_draft.description_ptbr,
            tags=list(latest_draft.tags or []),
            visibility=visibility,
            video_path=video.source_path,
        )
        video.status = VideoStatus.UPLOADED.value
        video.uploaded_url = youtube_url

    db.commit()
    db.refresh(video)
    return video
