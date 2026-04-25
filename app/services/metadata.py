"""Video metadata generation workflow.

Pipeline orchestrated here:
  - Steam achievements lookup (for the recorded window)
  - Transcript excerpt + chapters (when available, via Ollama)
  - Ollama-driven PT-BR title / description / tags / thumbnail prompt
  - Template-based fallback when Ollama is disabled or unreachable

The fallback output is stable so legacy tests asserting exact strings like
"Gameplay PT-BR" and "Prompt thumbnail: ..." keep passing.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import ChannelDefaults, MetadataDraft, SeriesFolder, VideoAsset, VideoStatus
from app.services.channel import get_or_create_channel_defaults
from app.services.errors import ConflictError, NotFoundError, ValidationError
from app.services.game_defaults import get_game_tag_defaults
from app.services.media import EpisodeThumbnailRenderer
from app.services.ollama import OllamaClient, format_chapters_for_description



def _seconds_to_hhmmss(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _build_achievement_timeline(recorded_at, duration_sec, achievements_unlocked_detailed):
    if not recorded_at or not duration_sec or not achievements_unlocked_detailed:
        return []
    end_at = recorded_at + timedelta(seconds=int(duration_sec))
    timeline = []
    for ach in achievements_unlocked_detailed or []:
        unlocked_at = ach.get('unlockedAt') or ach.get('unlock_time') or ach.get('unlockTime')
        if not unlocked_at:
            continue
        try:
            ts = datetime.fromisoformat(str(unlocked_at).replace('Z', '+00:00'))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if recorded_at <= ts <= end_at:
            offset = int((ts - recorded_at).total_seconds())
            timeline.append({
                'timecode': _seconds_to_hhmmss(offset),
                'name': ach.get('displayName') or ach.get('name') or ach.get('apiName') or 'Achievement',
                'description': ach.get('description') or '',
            })
    return timeline
from app.services.steam import get_achievements_for_window
from app.services.transcription import load_transcript
from app.services.youtube_publish import upload_video_to_youtube
from app.time_utils import format_datetime_ny


def _append_achievement_timeline(description: str | None, achievement_timeline) -> str | None:
    if not description or not achievement_timeline:
        return description
    lines = [description.rstrip(), '', 'Achievements during this video:']
    for ach in achievement_timeline:
        extra = f" — {ach['description']}" if ach.get('description') else ''
        lines.append(f"- [{ach['timecode']}] {ach['name']}{extra}")
    return "\n".join(lines).strip()


logger = get_logger(__name__)


def get_latest_draft(video: VideoAsset) -> MetadataDraft | None:
    if not video.drafts:
        return None
    return sorted(video.drafts, key=lambda draft: (draft.version, draft.created_at), reverse=True)[0]


def get_latest_active_draft(video: VideoAsset) -> MetadataDraft | None:
    active = [draft for draft in video.drafts if draft.is_active]
    if not active:
        return get_latest_draft(video)
    return sorted(active, key=lambda draft: (draft.version, draft.created_at), reverse=True)[0]


class MetadataWorkflowService:
    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings

    # ---- main entry points -------------------------------------------
    def generate_draft(self, video_id: str) -> MetadataDraft:
        settings = self._require_settings()
        video = self._get_video(video_id)
        folder = self._get_folder(video.folder_id)
        defaults = get_or_create_channel_defaults(self.db, settings)

        self._sync_steam_achievements(folder, video)
        self.db.refresh(video)

        episode_number = self._resolve_episode_number(video)
        transcript = load_transcript(video)
        per_game_tags = self._per_game_tags(folder)
        achievements = self._achievement_names(video)

        ollama = OllamaClient(settings)
        content = ollama.generate_video_content(
            game_name=folder.name,
            episode_number=episode_number,
            duration_minutes=(video.duration_sec // 60) if video.duration_sec else None,
            recorded_at_label=format_datetime_ny(video.recorded_at) if video.recorded_at else None,
            channel_language=defaults.language,
            pc_config=defaults.pc_config,
            default_description_block=defaults.default_description_block,
            default_tags=list(defaults.default_tags or []),
            per_game_tags=per_game_tags,
            achievements=achievements,
            transcript_excerpt=(transcript or {}).get("text"),
            thumbnail_hint=video.thumbnail_prompt,
        )

        chapters: list[dict[str, Any]] = content.chapters
        if transcript and not chapters and ollama.is_enabled():
            chapters = ollama.generate_chapters(
                game_name=folder.name,
                episode_number=episode_number,
                duration_seconds=int(video.duration_sec or 0),
                transcript_segments=transcript.get("segments") or [],
            )

        description = self._compose_description(
            base_description=content.description,
            chapters=chapters,
            defaults=defaults,
            thumbnail_prompt=video.thumbnail_prompt,
            used_fallback=content.used_fallback,
            playtime=_playtime_minutes(video),
        )

        tags = self._finalize_tags(folder, defaults, content.tags, per_game_tags)
        title = self._finalize_title(content.title, folder, episode_number, content.used_fallback)

        for draft in video.drafts:
            draft.is_active = False

        draft = MetadataDraft(
            video_id=video.id,
            title_ptbr=title,
            description_ptbr=description,
            tags=tags,
            chapters=chapters,
            thumbnail_path=self._generate_thumbnail(video, episode_number),
            thumbnail_prompt=content.thumbnail_prompt,
            model_provider="ollama" if not content.used_fallback else "template",
            model_name=content.model,
            language=defaults.language,
            version=self._next_draft_version(video),
            is_active=True,
        )

        video.status = VideoStatus.DRAFT_READY.value
        video.chapters = chapters
        self.db.add(draft)
        self.db.commit()
        self.db.refresh(draft)
        logger.info(
            "draft_generated",
            extra={
                "video_id": video.id,
                "used_fallback": content.used_fallback,
                "chapters": len(chapters),
                "tags": len(tags),
            },
        )
        return draft

    def update_video_settings(
        self,
        video_id: str,
        series_number: int | None,
        thumbnail_prompt: str | None,
    ) -> VideoAsset:
        video = self._get_video(video_id)
        if series_number is not None and series_number < 1:
            raise ValidationError("series_number must be greater than zero")

        video.series_number = series_number
        cleaned_prompt = (thumbnail_prompt or "").strip()
        if cleaned_prompt.lower() in {"undefined", "null", "none"}:
            cleaned_prompt = ""
        video.thumbnail_prompt = cleaned_prompt or None

        self.db.commit()
        self.db.refresh(video)
        return video

    def approve(self, video_id: str) -> VideoAsset:
        video = self._get_video(video_id)
        video.status = VideoStatus.APPROVED.value
        self.db.commit()
        self.db.refresh(video)
        return video

    def reject(self, video_id: str) -> VideoAsset:
        video = self._get_video(video_id)
        video.status = VideoStatus.INGESTED.value
        self.db.commit()
        self.db.refresh(video)
        return video

    def upload(self, video_id: str) -> VideoAsset:
        settings = self._require_settings()
        video = self._get_video(video_id)
        if video.status != VideoStatus.APPROVED.value:
            raise ConflictError("Video must be approved before upload")

        latest_draft = get_latest_draft(video)
        if latest_draft is None:
            latest_draft = self.generate_draft(video_id)
            video = self._get_video(video_id)

        defaults = get_or_create_channel_defaults(self.db, settings)
        visibility = defaults.default_visibility if defaults else "private"

        if settings.dry_run:
            video.status = VideoStatus.UPLOADED.value
            video.uploaded_url = f"https://youtube.com/watch?v=mock-{video.id[:8]}"
        else:
            video.uploaded_url = upload_video_to_youtube(
                settings,
                title=latest_draft.title_ptbr,
                description=latest_draft.description_ptbr,
                tags=list(latest_draft.tags or []),
                visibility=visibility,
                video_path=video.source_path,
            )
            video.status = VideoStatus.UPLOADED.value

        self.db.commit()
        self.db.refresh(video)
        return video

    # ---- composition helpers -----------------------------------------
    def _finalize_title(
        self,
        llm_title: str,
        folder: SeriesFolder,
        episode_number: int,
        used_fallback: bool,
    ) -> str:
        if used_fallback:
            return f"{folder.name} Gameplay PT-BR | Episodio {episode_number:02d}"

        title = (llm_title or "").strip() or f"{folder.name} Gameplay PT-BR"
        episode_suffix = f"Episodio {episode_number:02d}"
        if episode_suffix.lower() not in title.lower():
            title = f"{title} | {episode_suffix}"
        return title[:100]

    def _compose_description(
        self,
        *,
        base_description: str,
        chapters: list[dict[str, Any]],
        defaults: ChannelDefaults,
        thumbnail_prompt: str | None,
        used_fallback: bool,
        playtime: int | None,
    ) -> str:
        base = (base_description or "").strip()
        pieces: list[str] = [base] if base else []

        chapters_block = format_chapters_for_description(chapters)
        if chapters_block and chapters_block not in base:
            pieces.append(chapters_block)

        suffix_lines: list[str] = []
        if playtime is not None:
            suffix_lines.append(f"Playtime Steam no periodo: {playtime} minutos")
        if thumbnail_prompt:
            suffix_lines.append(f"Prompt thumbnail: {thumbnail_prompt}")

        defaults_block = (defaults.default_description_block or "").strip()
        if defaults_block and defaults_block not in base:
            if suffix_lines:
                suffix_lines.append("")
            suffix_lines.append(defaults_block)

        if suffix_lines:
            pieces.append("\n".join(suffix_lines))

        if not used_fallback:
            pieces.append(
                "Canal: https://www.youtube.com/@aggresiveHamster\n#GameplayPTBR #SemComentarios"
            )

        return "\n\n".join(piece for piece in pieces if piece).strip()

    def _finalize_tags(
        self,
        folder: SeriesFolder,
        defaults: ChannelDefaults,
        llm_tags: list[str],
        per_game_tags: list[str],
    ) -> list[str]:
        base = [folder.name.lower(), "gameplay", "sem comentarios", "pt-br"]
        merged = base + list(llm_tags or []) + list(defaults.default_tags or []) + per_game_tags

        normalized: list[str] = []
        seen: set[str] = set()
        for tag in merged:
            clean = " ".join(str(tag).split()).strip()
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(clean)
        return normalized[:15]

    def _per_game_tags(self, folder: SeriesFolder) -> list[str]:
        per_game = get_game_tag_defaults()
        folder_key = folder.name.strip().lower()
        collected: list[str] = []
        for game_name, game_tags in per_game.items():
            game_key = game_name.strip().lower()
            if game_key and (game_key == folder_key or game_key in folder_key or folder_key in game_key):
                collected.extend(game_tags)
        return collected

    def _achievement_names(self, video: VideoAsset) -> list[str]:
        payload = video.session_payload or {}
        items = payload.get("achievements_unlocked") or []
        return [str(name) for name in items if name]

    def _require_settings(self) -> Settings:
        if self.settings is None:
            raise RuntimeError("Settings are required for this operation")
        return self.settings

    def _get_video(self, video_id: str) -> VideoAsset:
        video = self.db.get(VideoAsset, video_id)
        if video is None:
            raise NotFoundError("Video not found")
        return video

    def _get_folder(self, folder_id: str) -> SeriesFolder:
        folder = self.db.get(SeriesFolder, folder_id)
        if folder is None:
            raise NotFoundError("Folder not found")
        return folder

    def _next_draft_version(self, video: VideoAsset) -> int:
        latest = get_latest_draft(video)
        return 1 if latest is None else latest.version + 1

    def _resolve_episode_number(self, video: VideoAsset) -> int:
        if video.series_number and video.series_number > 0:
            return video.series_number
        return self._extract_episode_number(video)

    def _extract_episode_number(self, video: VideoAsset) -> int:
        videos = self.db.execute(
            select(VideoAsset)
            .where(VideoAsset.folder_id == video.folder_id)
            .order_by(
                VideoAsset.recorded_at.is_(None),
                VideoAsset.recorded_at.asc(),
                VideoAsset.created_at.asc(),
            )
        ).scalars()

        for index, item in enumerate(videos, start=1):
            if item.id == video.id:
                return index
        return 1

    def _sync_steam_achievements(self, folder: SeriesFolder, video: VideoAsset) -> None:
        if not folder.steam_app_id or not video.recorded_at or not video.duration_sec:
            return

        settings = self._require_settings()
        start_utc = video.recorded_at
        end_utc = video.recorded_at + timedelta(seconds=max(1, int(video.duration_sec)))
        matched = get_achievements_for_window(settings, int(folder.steam_app_id), start_utc, end_utc)

        payload = dict(video.session_payload or {})
        payload["achievements_unlocked"] = [item.get("name") for item in matched if item.get("name")]
        payload["achievements_unlocked_detailed"] = matched
        video.session_payload = payload
        self.db.commit()

    def _generate_thumbnail(self, video: VideoAsset, episode_number: int) -> str | None:
        return EpisodeThumbnailRenderer.render(
            video_path=Path(video.source_path),
            output_path=Path(self._require_settings().artifacts_root) / "thumbnails" / f"{video.id}.jpg",
            episode_number=episode_number,
            thumbnail_prompt=video.thumbnail_prompt,
        )


def _playtime_minutes(video: VideoAsset) -> int | None:
    payload = video.session_payload or {}
    value = payload.get("playtime_minutes")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---- module-level facade used by routers/tests --------------------------
def generate_metadata_draft(db: Session, settings: Settings, video_id: str) -> MetadataDraft:
    return MetadataWorkflowService(db, settings).generate_draft(video_id)


def update_video_settings(
    db: Session,
    video_id: str,
    series_number: int | None,
    thumbnail_prompt: str | None,
) -> VideoAsset:
    return MetadataWorkflowService(db).update_video_settings(video_id, series_number, thumbnail_prompt)


def approve_video(db: Session, video_id: str) -> VideoAsset:
    return MetadataWorkflowService(db).approve(video_id)


def reject_video(db: Session, video_id: str) -> VideoAsset:
    return MetadataWorkflowService(db).reject(video_id)


def upload_video(db: Session, settings: Settings, video_id: str) -> VideoAsset:
    return MetadataWorkflowService(db, settings).upload(video_id)
