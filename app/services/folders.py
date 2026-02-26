from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ChannelDefaults, SeriesFolder, VideoAsset

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}


def ensure_channel_defaults(db: Session, settings: Settings) -> ChannelDefaults:
    defaults = db.get(ChannelDefaults, 1)
    if defaults:
        return defaults

    defaults = ChannelDefaults(
        id=1,
        channel_name="Meu Canal de Gameplay",
        language=settings.default_language,
        default_tags=["gameplay", "sem comentarios", "pt-br"],
        pc_config="RTX",
        default_description_block="Gameplay sem comentarios. Gravado em PC com RTX.",
        default_visibility="private",
        updated_at=datetime.now(timezone.utc),
    )
    db.add(defaults)
    db.flush()
    return defaults


def to_series_slug(name: str) -> str:
    value = slugify(name, lowercase=True) or "serie"
    return value[:120]


def _unique_slug(base_slug: str, used_slugs: set[str]) -> str:
    slug = base_slug
    index = 2
    while slug in used_slugs:
        slug = f"{base_slug}-{index}"
        index += 1
    used_slugs.add(slug)
    return slug


def parse_recorded_at_from_filename(filename: str) -> datetime | None:
    # Supports: 2026-02-26_21-10-00 or 2026-02-26 21-10-00
    match = re.search(r"(\d{4}-\d{2}-\d{2})[ _](\d{2})[-:](\d{2})[-:](\d{2})", filename)
    if not match:
        return None

    date_part, hour, minute, second = match.groups()
    value = f"{date_part}T{hour}:{minute}:{second}+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_session_payload(video_path: Path) -> dict:
    sidecar_candidates = [
        video_path.with_suffix(video_path.suffix + ".session.json"),
        video_path.with_suffix(video_path.suffix + ".ps.json"),
        video_path.with_suffix(".session.json"),
    ]

    for candidate in sidecar_candidates:
        if not candidate.exists() or not candidate.is_file():
            continue

        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    return {}


def probe_duration_seconds(video_path: Path) -> int | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None

    value = (result.stdout or "").strip()
    if not value:
        return None

    try:
        return int(float(value))
    except ValueError:
        return None


def _resolve_slug_for_folder(
    folder_name: str,
    existing_by_path: dict[str, SeriesFolder],
    existing_by_slug: dict[str, SeriesFolder],
    folder_path: str,
    reserved_slugs: set[str],
) -> str:
    folder = existing_by_path.get(folder_path)
    if folder and folder.slug not in reserved_slugs:
        reserved_slugs.add(folder.slug)
        return folder.slug

    base_slug = to_series_slug(folder_name)
    candidate = base_slug
    idx = 2
    while True:
        existing = existing_by_slug.get(candidate)
        if candidate not in reserved_slugs and (not existing or existing.path == folder_path):
            reserved_slugs.add(candidate)
            return candidate
        candidate = f"{base_slug}-{idx}"
        idx += 1


def _normalize_for_match(value: str) -> str:
    return slugify(value or "", lowercase=True).replace("-", "")


def _find_steam_match(folder_name: str, steam_games: list[dict] | None) -> dict | None:
    if not steam_games:
        return None

    folder_norm = _normalize_for_match(folder_name)
    if not folder_norm:
        return None

    for game in steam_games:
        game_name = str(game.get("name") or "")
        game_norm = _normalize_for_match(game_name)
        if not game_norm:
            continue
        if folder_norm == game_norm:
            return game

    for game in steam_games:
        game_name = str(game.get("name") or "")
        game_norm = _normalize_for_match(game_name)
        if not game_norm:
            continue
        if folder_norm in game_norm or game_norm in folder_norm:
            return game

    return None


def _apply_folder_steam_match(folder: SeriesFolder, folder_name: str, steam_games: list[dict] | None) -> None:
    if folder.steam_app_id and folder.steam_game_name:
        return

    match = _find_steam_match(folder_name, steam_games)
    if not match:
        return

    try:
        steam_app_id = int(match.get("appid")) if match.get("appid") is not None else None
    except (TypeError, ValueError):
        steam_app_id = None

    if steam_app_id:
        folder.steam_app_id = steam_app_id
    folder.steam_game_name = str(match.get("name") or folder_name)


def sync_folders_and_videos(
    db: Session,
    settings: Settings,
    root_path: str | None = None,
    steam_games: list[dict] | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    root = Path(root_path or settings.video_root)
    root.mkdir(parents=True, exist_ok=True)

    existing_folders = db.execute(select(SeriesFolder)).scalars().all()
    existing_by_path = {folder.path: folder for folder in existing_folders}
    existing_by_slug = {folder.slug: folder for folder in existing_folders}
    reserved_slugs: set[str] = set()

    discovered_dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
    discovered_paths: set[str] = set()
    new_folders = 0
    reactivated_folders = 0
    new_videos = 0

    for directory in discovered_dirs:
        abs_path = str(directory.resolve())
        discovered_paths.add(abs_path)

        folder = existing_by_path.get(abs_path)
        folder_name = directory.name
        slug = _resolve_slug_for_folder(folder_name, existing_by_path, existing_by_slug, abs_path, reserved_slugs)

        if not folder:
            folder = SeriesFolder(
                name=folder_name,
                slug=slug,
                path=abs_path,
                series_url=f"/series/{slug}",
                steam_app_id=None,
                steam_game_name=None,
                active=True,
                last_scan_at=now,
            )
            db.add(folder)
            db.flush()
            existing_by_path[abs_path] = folder
            existing_by_slug[slug] = folder
            new_folders += 1
        else:
            if not folder.active:
                reactivated_folders += 1
            folder.name = folder_name
            folder.slug = slug
            folder.series_url = f"/series/{slug}"
            folder.active = True
            folder.last_scan_at = now

        _apply_folder_steam_match(folder, folder_name, steam_games)

        found_video_paths = set()
        files = sorted([f for f in directory.iterdir() if f.is_file()], key=lambda f: f.name.lower())
        for file_path in files:
            if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue

            full_path = str(file_path.resolve())
            found_video_paths.add(full_path)

            existing_video = db.execute(
                select(VideoAsset).where(VideoAsset.source_path == full_path)
            ).scalar_one_or_none()
            if existing_video:
                if existing_video.folder_id != folder.id:
                    existing_video.folder_id = folder.id
                continue

            session_payload = load_session_payload(file_path)
            recorded_at = None
            if session_payload.get("recorded_at"):
                try:
                    recorded_at = datetime.fromisoformat(session_payload["recorded_at"])
                except ValueError:
                    recorded_at = None
            if not recorded_at:
                recorded_at = parse_recorded_at_from_filename(file_path.name)

            series_number = session_payload.get("series_number")
            if not isinstance(series_number, int) or series_number < 1:
                series_number = None

            thumbnail_prompt = session_payload.get("thumbnail_prompt")
            if not isinstance(thumbnail_prompt, str):
                thumbnail_prompt = None
            elif not thumbnail_prompt.strip():
                thumbnail_prompt = None
            else:
                thumbnail_prompt = thumbnail_prompt.strip()

            new_video = VideoAsset(
                folder_id=folder.id,
                filename=file_path.name,
                source_path=full_path,
                recorded_at=recorded_at,
                duration_sec=probe_duration_seconds(file_path),
                series_number=series_number,
                thumbnail_prompt=thumbnail_prompt,
                status="INGESTED",
                language=settings.default_language,
                session_payload=session_payload,
            )
            db.add(new_video)
            new_videos += 1

        # Ensure newly added videos are visible to the count query in this transaction.
        db.flush()
        folder.video_count = db.execute(
            select(func.count(VideoAsset.id)).where(VideoAsset.folder_id == folder.id)
        ).scalar_one()

    deactivated_folders = 0
    for folder in existing_folders:
        if folder.path not in discovered_paths and folder.active:
            folder.active = False
            folder.last_scan_at = now
            deactivated_folders += 1

    db.commit()

    return {
        "discovered_folders": len(discovered_dirs),
        "new_folders": new_folders,
        "reactivated_folders": reactivated_folders,
        "deactivated_folders": deactivated_folders,
        "new_videos": new_videos,
        "scanned_at": now,
    }


def update_folder_steam_link(
    db: Session,
    folder_id: str,
    steam_app_id: int | None,
    steam_game_name: str | None,
) -> SeriesFolder:
    folder = db.get(SeriesFolder, folder_id)
    if not folder:
        raise ValueError("Folder not found")

    folder.steam_app_id = steam_app_id
    cleaned_name = (steam_game_name or "").strip()
    folder.steam_game_name = cleaned_name or None
    db.commit()
    db.refresh(folder)
    return folder
