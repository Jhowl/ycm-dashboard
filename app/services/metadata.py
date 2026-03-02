from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ChannelDefaults, MetadataDraft, SeriesFolder, VideoAsset, VideoStatus
from app.services.channel import get_or_create_channel_defaults
from app.services.errors import ConflictError, NotFoundError, ValidationError
from app.services.game_defaults import get_game_tag_defaults
from app.services.media import EpisodeThumbnailRenderer
from app.services.steam import get_achievements_for_window
from app.services.youtube_publish import upload_video_to_youtube


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

    def generate_draft(self, video_id: str) -> MetadataDraft:
        settings = self._require_settings()
        video = self._get_video(video_id)
        folder = self._get_folder(video.folder_id)
        defaults = get_or_create_channel_defaults(self.db, settings)

        self._sync_steam_achievements(folder, video)

        episode_number = self._resolve_episode_number(video)
        title = self._build_title(folder, video, episode_number)
        description = self._build_description(folder, video, defaults, episode_number)
        tags = self._build_tags(folder, defaults)

        for draft in video.drafts:
            draft.is_active = False

        draft = MetadataDraft(
            video_id=video.id,
            title_ptbr=title,
            description_ptbr=description,
            tags=tags,
            thumbnail_path=self._generate_thumbnail(video, episode_number),
            model_provider=settings.default_model_provider,
            language=defaults.language,
            version=self._next_draft_version(video),
            is_active=True,
        )

        video.status = VideoStatus.DRAFT_READY.value
        self.db.add(draft)
        self.db.commit()
        self.db.refresh(draft)
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
            .order_by(VideoAsset.recorded_at.is_(None), VideoAsset.recorded_at.asc(), VideoAsset.created_at.asc())
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

    def _build_title(self, folder: SeriesFolder, video: VideoAsset, episode_number: int) -> str:
        return f"{folder.name} Gameplay PT-BR | Episodio {episode_number:02d}"

    def _build_tags(self, folder: SeriesFolder, defaults: ChannelDefaults) -> list[str]:
        tags = [folder.name.lower(), "gameplay", "sem comentarios", "pt-br", *defaults.default_tags]

        per_game = get_game_tag_defaults()
        folder_key = folder.name.strip().lower()
        for game_name, game_tags in per_game.items():
            game_key = game_name.strip().lower()
            if game_key and (game_key == folder_key or game_key in folder_key or folder_key in game_key):
                tags.extend(game_tags)

        normalized: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            clean = " ".join(tag.split()).strip()
            if not clean:
                continue
            lowered = clean.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(clean)
        return normalized[:15]

    def _build_description(
        self,
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
        if "resident evil" in folder_name or "requiem" in folder_name:
            lines = [
                (
                    f"No episódio {episode_number:02d} de Resident Evil Requiem (Resident Evil 9), "
                    "acompanhe gameplay sem comentários em PT-BR com foco na progressão da campanha, "
                    "exploração, combate e clima de survival horror."
                ),
                "",
                "Conteúdo deste vídeo:",
                "- Gameplay sem comentários (no commentary)",
                "- Trechos com Claire e Leon",
                "- Exploração + combate + progressão de história",
                "- Conquistas desbloqueadas durante a gameplay",
                "",
                f"Jogo: {folder.name}",
                f"Idioma: {defaults.language}",
                f"Plataforma: PC ({defaults.pc_config})",
                f"Duração: ~{duration_minutes} min" if duration_minutes is not None else "Duração: indisponível",
            ]
            if playtime is not None:
                lines.append(f"Playtime Steam no período: {playtime} minutos")
            lines.append(self._achievements_line(achievements))
            if video.thumbnail_prompt:
                lines.append(f"Prompt thumbnail: {video.thumbnail_prompt}")
            lines.extend(
                [
                    "",
                    defaults.default_description_block,
                    "",
                    "Se curtir, deixa o like e se inscreve para acompanhar a série completa.",
                    "Canal: https://www.youtube.com/@aggresiveHamster",
                    "",
                    "#ResidentEvilRequiem #ResidentEvil9 #GameplayPTBR #SemComentarios #SurvivalHorror",
                ]
            )
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
        lines.append(self._achievements_line(achievements))
        if video.thumbnail_prompt:
            lines.append(f"Prompt thumbnail: {video.thumbnail_prompt}")
        lines.append(f"PC: {defaults.pc_config}")
        lines.append("")
        lines.append(defaults.default_description_block)
        return "\n".join(lines)

    def _achievements_line(self, achievements: list) -> str:
        if achievements:
            return "Conquistas desbloqueadas: " + ", ".join(str(item) for item in achievements)
        return "Conquistas desbloqueadas: sem novas conquistas registradas"

    def _generate_thumbnail(self, video: VideoAsset, episode_number: int) -> str | None:
        return EpisodeThumbnailRenderer.render(
            video_path=Path(video.source_path),
            output_path=Path(self._require_settings().artifacts_root) / "thumbnails" / f"{video.id}.jpg",
            episode_number=episode_number,
            thumbnail_prompt=video.thumbnail_prompt,
        )


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
