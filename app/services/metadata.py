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
from app.services.ollama import OllamaClient, OllamaContent, format_chapters_for_description
from app.services import ollama_progress as progress



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
        transcript_text = (transcript or {}).get("text") if transcript else None
        per_game_tags = self._per_game_tags(folder)
        achievements = self._achievement_names(video)

        ollama = OllamaClient(settings)

        # Reset progress at start so the UI chip shows live status.
        progress.reset(self.db, video.id)

        # Always compute the deterministic fallback first so any failed step
        # has a sensible value to fall back to.
        fallback = ollama._fallback_content(
            game_name=folder.name,
            episode_number=episode_number,
            default_description_block=defaults.default_description_block,
            default_tags=list(defaults.default_tags or []),
            per_game_tags=per_game_tags,
            achievements=achievements,
            thumbnail_hint=video.thumbnail_prompt,
        )

        used_fallback = not ollama.is_enabled()
        title_val = fallback.title
        tags_val = list(fallback.tags)
        thumb_prompt_val = fallback.thumbnail_prompt
        desc_val = fallback.description

        if ollama.is_enabled():
            # 1. title
            progress.mark_running(self.db, video.id, "title")
            try:
                t = ollama.gen_title(
                    game_name=folder.name,
                    episode_number=episode_number,
                    transcript_excerpt=transcript_text,
                    achievements=achievements,
                )
                if t:
                    title_val = t
                    progress.mark_done(self.db, video.id, "title")
                else:
                    progress.mark_failed(self.db, video.id, "title", "no response")
                    used_fallback = True
            except Exception as exc:
                progress.mark_failed(self.db, video.id, "title", str(exc))
                used_fallback = True

            # 2. tags
            progress.mark_running(self.db, video.id, "tags")
            try:
                tg = ollama.gen_tags(
                    game_name=folder.name,
                    default_tags=list(defaults.default_tags or []),
                    per_game_tags=per_game_tags,
                    transcript_excerpt=transcript_text,
                    achievements=achievements,
                )
                if tg:
                    tags_val = tg
                    progress.mark_done(self.db, video.id, "tags")
                else:
                    progress.mark_failed(self.db, video.id, "tags", "no response")
            except Exception as exc:
                progress.mark_failed(self.db, video.id, "tags", str(exc))

            # 3. thumbnail_prompt
            progress.mark_running(self.db, video.id, "thumbnail_prompt")
            try:
                tp = ollama.gen_thumbnail_prompt(
                    game_name=folder.name,
                    episode_number=episode_number,
                    hint=video.thumbnail_prompt,
                )
                if tp:
                    thumb_prompt_val = tp
                    progress.mark_done(self.db, video.id, "thumbnail_prompt")
                else:
                    progress.mark_failed(self.db, video.id, "thumbnail_prompt", "no response")
            except Exception as exc:
                progress.mark_failed(self.db, video.id, "thumbnail_prompt", str(exc))
        else:
            for step in ("title", "tags", "thumbnail_prompt"):
                progress.mark_skipped(self.db, video.id, step)

        # 4. chapters — only if we have a transcript and Ollama is on
        chapters: list[dict[str, Any]] = []
        if ollama.is_enabled() and transcript:
            progress.mark_running(self.db, video.id, "chapters")
            try:
                chapters = ollama.generate_chapters(
                    game_name=folder.name,
                    episode_number=episode_number,
                    duration_seconds=int(video.duration_sec or 0),
                    transcript_segments=transcript.get("segments") or [],
                )
                progress.mark_done(self.db, video.id, "chapters")
            except Exception as exc:
                progress.mark_failed(self.db, video.id, "chapters", str(exc))
        else:
            progress.mark_skipped(self.db, video.id, "chapters")

        # 5. description (depends on chapters)
        if ollama.is_enabled():
            progress.mark_running(self.db, video.id, "description")
            try:
                d = ollama.gen_description(
                    game_name=folder.name,
                    episode_number=episode_number,
                    transcript_excerpt=transcript_text,
                    achievements=achievements,
                    chapters=chapters,
                    default_block=defaults.default_description_block,
                )
                if d:
                    desc_val = d
                    progress.mark_done(self.db, video.id, "description")
                else:
                    progress.mark_failed(self.db, video.id, "description", "no response")
                    used_fallback = True
            except Exception as exc:
                progress.mark_failed(self.db, video.id, "description", str(exc))
                used_fallback = True
        else:
            progress.mark_skipped(self.db, video.id, "description")

        content = OllamaContent(
            title=title_val,
            description=desc_val,
            tags=tags_val,
            thumbnail_prompt=thumb_prompt_val,
            chapters=chapters,
            used_fallback=used_fallback,
            model=settings.ollama_model if ollama.is_enabled() else None,
        )

        description = self._compose_description(
            base_description=content.description,
            chapters=chapters,
            defaults=defaults,
            thumbnail_prompt=video.thumbnail_prompt,
            used_fallback=content.used_fallback,
            playtime=_playtime_minutes(video),
            timed_achievements=self._achievement_timed(video),
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
        timed_achievements: list[tuple[int, str]] | None = None,
    ) -> str:
        base = (base_description or "").strip()
        pieces: list[str] = [base] if base else []

        # YouTube chapters block (00:00 ...).
        chapters_block = format_chapters_for_description(chapters)
        if chapters_block and chapters_block not in base:
            pieces.append(chapters_block)

        # Timed achievements block (MM:SS Name) — appended below the description.
        # If we have timing data, drop the legacy single-line achievements summary
        # from base/fallback to avoid duplicate listings.
        ach_block = format_achievements_with_timing(timed_achievements or [])
        if ach_block:
            cleaned: list[str] = []
            for piece in pieces:
                lines = [
                    line for line in piece.splitlines()
                    if not line.startswith("Conquistas desbloqueadas: ")
                ]
                cleaned.append("\n".join(lines).strip())
            pieces = [p for p in cleaned if p]
            pieces.append(ach_block)

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

    def _achievement_timed(self, video: VideoAsset) -> list[tuple[int, str]]:
        """Return [(offset_seconds, name), ...] sorted by offset."""
        payload = video.session_payload or {}
        items = payload.get("achievements_unlocked_detailed") or []
        out: list[tuple[int, str]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            try:
                offset = int(it.get("offset_seconds") or 0)
            except (TypeError, ValueError):
                offset = 0
            out.append((max(0, offset), name))
        out.sort(key=lambda x: x[0])
        return out

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

        # Annotate each achievement with its offset (sec) from the start of the video.
        start_ts = int(start_utc.timestamp())
        for item in matched:
            unlock_ts = int(item.get("unlocktime", 0) or 0)
            offset = max(0, unlock_ts - start_ts) if unlock_ts else 0
            item["offset_seconds"] = offset

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


def format_achievements_with_timing(items: list[tuple[int, str]]) -> str:
    """Render YouTube-style achievement timeline: 'MM:SS Name' lines."""
    if not items:
        return ""
    lines = ["Conquistas desbloqueadas:"]
    for offset, name in items:
        try:
            sec = int(offset)
        except (TypeError, ValueError):
            sec = 0
        if sec < 0:
            sec = 0
        hh = sec // 3600
        mm = (sec % 3600) // 60
        ss = sec % 60
        stamp = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        lines.append(f"{stamp} {name}")
    return "\n".join(lines)


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
