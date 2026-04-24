"""Filesystem → DB sync for series folders and video assets."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import SeriesFolder, VideoAsset
from app.services.channel import get_or_create_channel_defaults
from app.services.errors import NotFoundError
from app.services.media import VideoProbe
from app.services.steam import auto_match_folder_by_playtime

logger = get_logger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}


@dataclass(slots=True)
class FolderSyncStats:
    discovered_folders: int = 0
    new_folders: int = 0
    reactivated_folders: int = 0
    deactivated_folders: int = 0
    new_videos: int = 0
    auto_linked_folders: int = 0
    scanned_at: datetime | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class FolderSyncService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        root_path: str | None = None,
        steam_games: list[dict] | None = None,
        owned_games: list[dict] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.root = Path(root_path or settings.video_root)
        self.steam_recent = steam_games or []
        self.steam_owned = owned_games or []
        self.now = datetime.now(timezone.utc)
        self.stats = FolderSyncStats(scanned_at=self.now)

        self.root.mkdir(parents=True, exist_ok=True)
        self.existing_folders = self.db.execute(select(SeriesFolder)).scalars().all()
        self.existing_by_path = {folder.path: folder for folder in self.existing_folders}
        self.existing_by_slug = {folder.slug: folder for folder in self.existing_folders}
        self.reserved_slugs: set[str] = set()

    def sync(self) -> FolderSyncStats:
        discovered_dirs = sorted(
            (path for path in self.root.iterdir() if path.is_dir()),
            key=lambda path: path.name.lower(),
        )
        discovered_paths: set[str] = set()
        self.stats.discovered_folders = len(discovered_dirs)

        for directory in discovered_dirs:
            folder = self._sync_folder(directory)
            discovered_paths.add(folder.path)
            self._sync_videos_for_folder(folder, directory)

        self._deactivate_missing_folders(discovered_paths)
        self.db.commit()
        logger.info(
            "folder_sync_done",
            extra={
                "discovered": self.stats.discovered_folders,
                "new": self.stats.new_folders,
                "new_videos": self.stats.new_videos,
                "auto_linked": self.stats.auto_linked_folders,
            },
        )
        return self.stats

    def _sync_folder(self, directory: Path) -> SeriesFolder:
        folder_path = str(directory.resolve())
        folder_name = directory.name
        folder = self.existing_by_path.get(folder_path)
        slug = self._resolve_slug(folder_name=folder_name, folder_path=folder_path)

        if folder is None:
            folder = SeriesFolder(
                name=folder_name,
                slug=slug,
                path=folder_path,
                series_url=f"/series/{slug}",
                active=True,
                last_scan_at=self.now,
            )
            self.db.add(folder)
            self.db.flush()
            self.existing_by_path[folder_path] = folder
            self.existing_by_slug[slug] = folder
            self.stats.new_folders += 1
        else:
            if not folder.active:
                self.stats.reactivated_folders += 1
            folder.name = folder_name
            folder.slug = slug
            folder.series_url = f"/series/{slug}"
            folder.active = True
            folder.last_scan_at = self.now

        self._apply_folder_steam_match(folder, folder_name)
        return folder

    def _sync_videos_for_folder(self, folder: SeriesFolder, directory: Path) -> None:
        for file_path in sorted(
            (path for path in directory.iterdir() if path.is_file()),
            key=lambda path: path.name.lower(),
        ):
            if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            self._ingest_video_file(folder, file_path)

        self.db.flush()
        folder.video_count = self.db.execute(
            select(func.count(VideoAsset.id)).where(VideoAsset.folder_id == folder.id)
        ).scalar_one()

    def _ingest_video_file(self, folder: SeriesFolder, file_path: Path) -> None:
        full_path = str(file_path.resolve())
        existing_video = self.db.execute(
            select(VideoAsset).where(VideoAsset.source_path == full_path)
        ).scalar_one_or_none()
        if existing_video:
            if existing_video.folder_id != folder.id:
                existing_video.folder_id = folder.id
            return

        session_payload = load_session_payload(file_path)
        recorded_at = _resolve_recorded_at(file_path, session_payload)
        series_number = _normalize_series_number(session_payload.get("series_number"))
        thumbnail_prompt = _normalize_thumbnail_prompt(session_payload.get("thumbnail_prompt"))

        video = VideoAsset(
            folder_id=folder.id,
            filename=file_path.name,
            source_path=full_path,
            recorded_at=recorded_at,
            duration_sec=probe_duration_seconds(file_path),
            series_number=series_number,
            thumbnail_prompt=thumbnail_prompt,
            status="INGESTED",
            language=self.settings.default_language,
            session_payload=session_payload,
        )
        self.db.add(video)
        self.db.flush()
        self.stats.new_videos += 1

        self._maybe_schedule_transcription(video)

    def _maybe_schedule_transcription(self, video: VideoAsset) -> None:
        if not self.settings.whisper_enabled or not self.settings.whisper_auto_run:
            return
        try:
            # Lazy import to avoid circular dependency + keep tests quick.
            from worker.tasks import transcribe_video_task

            transcribe_video_task.delay(video.id)
            logger.info("transcribe_scheduled", extra={"video_id": video.id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcribe_schedule_failed", extra={"err": str(exc)[:200]})

    def _deactivate_missing_folders(self, discovered_paths: set[str]) -> None:
        for folder in self.existing_folders:
            if folder.path in discovered_paths or not folder.active:
                continue
            folder.active = False
            folder.last_scan_at = self.now
            self.stats.deactivated_folders += 1

    def _resolve_slug(self, *, folder_name: str, folder_path: str) -> str:
        folder = self.existing_by_path.get(folder_path)
        if folder and folder.slug not in self.reserved_slugs:
            self.reserved_slugs.add(folder.slug)
            return folder.slug

        base_slug = to_series_slug(folder_name)
        candidate = base_slug
        index = 2
        while True:
            existing = self.existing_by_slug.get(candidate)
            if candidate not in self.reserved_slugs and (existing is None or existing.path == folder_path):
                self.reserved_slugs.add(candidate)
                return candidate
            candidate = f"{base_slug}-{index}"
            index += 1

    def _apply_folder_steam_match(self, folder: SeriesFolder, folder_name: str) -> None:
        if folder.steam_app_id and folder.steam_game_name:
            return

        match = auto_match_folder_by_playtime(
            folder_name,
            recent_games=self.steam_recent,
            owned_games=self.steam_owned,
        )
        if not match:
            return

        try:
            steam_app_id = int(match.get("appid")) if match.get("appid") is not None else None
        except (TypeError, ValueError):
            steam_app_id = None

        if steam_app_id:
            folder.steam_app_id = steam_app_id
            folder.steam_game_name = str(match.get("name") or folder_name)
            self.stats.auto_linked_folders += 1
            logger.info(
                "folder_auto_linked",
                extra={"folder": folder_name, "app_id": steam_app_id, "game": match.get("name")},
            )


def ensure_channel_defaults(db: Session, settings: Settings):
    return get_or_create_channel_defaults(db, settings)


def to_series_slug(name: str) -> str:
    value = slugify(name, lowercase=True) or "serie"
    return value[:120]


def parse_recorded_at_from_filename(filename: str) -> datetime | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})[ _](\d{2})[-:](\d{2})[-:](\d{2})", filename)
    if not match:
        return None

    date_part, hour, minute, second = match.groups()
    try:
        return datetime.fromisoformat(f"{date_part}T{hour}:{minute}:{second}+00:00")
    except ValueError:
        return None


def load_session_payload(video_path: Path) -> dict:
    for candidate in (
        video_path.with_suffix(video_path.suffix + ".session.json"),
        video_path.with_suffix(video_path.suffix + ".ps.json"),
        video_path.with_suffix(".session.json"),
    ):
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def probe_duration_seconds(video_path: Path) -> int | None:
    return VideoProbe.duration_seconds(video_path)


def sync_folders_and_videos(
    db: Session,
    settings: Settings,
    root_path: str | None = None,
    steam_games: list[dict] | None = None,
    owned_games: list[dict] | None = None,
) -> dict:
    service = FolderSyncService(
        db, settings, root_path=root_path, steam_games=steam_games, owned_games=owned_games
    )
    return service.sync().to_dict()


def update_folder_steam_link(
    db: Session,
    folder_id: str,
    steam_app_id: int | None,
    steam_game_name: str | None,
) -> SeriesFolder:
    folder = db.get(SeriesFolder, folder_id)
    if not folder:
        raise NotFoundError("Folder not found")

    folder.steam_app_id = steam_app_id
    cleaned_name = (steam_game_name or "").strip()
    folder.steam_game_name = cleaned_name or None
    db.commit()
    db.refresh(folder)
    return folder


def _resolve_recorded_at(file_path: Path, session_payload: dict) -> datetime | None:
    payload_value = session_payload.get("recorded_at")
    if payload_value:
        try:
            return datetime.fromisoformat(str(payload_value))
        except ValueError:
            pass
    return parse_recorded_at_from_filename(file_path.name)


def _normalize_series_number(value: object) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    return None


def _normalize_thumbnail_prompt(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
