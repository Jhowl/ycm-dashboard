"""Trim short clips around each Steam achievement unlock and register them
as standalone uploadable VideoAssets.

For every successful cut we:
  1. ffmpeg-extract the clip into ``<series_folder.path>/cut/`` so it lives
     on the same host-mounted volume as the source video.
  2. Generate PT-BR SEO content (title / description / tags) — Ollama with
     deterministic template fallback.
  3. Force-append an "Exact moment" footer with the original-video timestamp
     so viewers can scrub back to context.
  4. Create (or upsert) a ``VideoAsset`` row pointing at the clip and an
     active ``MetadataDraft`` so the existing upload pipeline (approve →
     youtube_publish) just works for cuts.
  5. Drop a JSON sidecar next to the clip for offline auditing.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import (
    MetadataDraft,
    SeriesFolder,
    TranscriptStatus,
    VideoAsset,
    VideoStatus,
)
from app.services.channel import get_or_create_channel_defaults
from app.services.errors import NotFoundError, ValidationError
from app.services.game_defaults import get_game_tag_defaults
from app.services.ollama import OllamaClient

logger = get_logger(__name__)

DEFAULT_TRIM_SECONDS = 60
MIN_TRIM_SECONDS = 10
MAX_TRIM_SECONDS = 600


@dataclass(slots=True)
class TrimmedClip:
    filename: str
    clip_path: str
    sidecar_path: str
    clip_video_id: str
    parent_video_id: str
    achievement_name: str
    offset_seconds: int
    start_seconds: int
    duration_seconds: int
    title: str
    description: str
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def trim_clips_for_video(
    db: Session,
    settings: Settings,
    video_id: str,
    *,
    trim_seconds: int = DEFAULT_TRIM_SECONDS,
) -> list[TrimmedClip]:
    video = db.get(VideoAsset, video_id)
    if not video:
        raise NotFoundError("Video not found")

    folder = db.get(SeriesFolder, video.folder_id)
    if not folder:
        raise NotFoundError("Folder not found")

    source = Path(video.source_path)
    if not source.exists():
        raise ValidationError(f"Video file not found: {source}")

    detailed = (video.session_payload or {}).get("achievements_unlocked_detailed") or []
    timed = [item for item in detailed if isinstance(item, dict) and item.get("name")]
    if not timed:
        return []

    duration = max(MIN_TRIM_SECONDS, min(MAX_TRIM_SECONDS, int(trim_seconds or DEFAULT_TRIM_SECONDS)))
    pre_seconds = duration // 2
    post_seconds = duration - pre_seconds  # noqa: F841 (kept for clarity)

    defaults = get_or_create_channel_defaults(db, settings)
    per_game_tags = _per_game_tags(folder.name)
    ollama = OllamaClient(settings)

    cuts_root = Path(folder.path) / "cut"
    cuts_root.mkdir(parents=True, exist_ok=True)

    sorted_items = sorted(timed, key=lambda i: int(i.get("offset_seconds") or 0))
    short_id = video.id[:8]
    parent_filename = video.filename

    results: list[TrimmedClip] = []
    for idx, item in enumerate(sorted_items, start=1):
        offset = max(0, int(item.get("offset_seconds") or 0))
        start = max(0, offset - pre_seconds)
        ach_name = str(
            item.get("displayName") or item.get("name") or item.get("apiName") or "achievement"
        ).strip()
        ach_desc = str(item.get("description") or "").strip()
        timestamp_label = _hhmmss(offset)

        slug = slugify(f"{idx:02d}-{ach_name}")[:60] or f"clip-{idx:02d}"
        clip_path = cuts_root / f"{short_id}__{slug}.mp4"
        sidecar_path = clip_path.with_suffix(".json")

        if not _ffmpeg_extract(source, clip_path, start_seconds=start, duration_seconds=duration):
            logger.warning("trim_failed", extra={"video_id": video.id, "ach": ach_name})
            continue

        seo = _build_seo_content(
            ollama=ollama,
            game_name=folder.name,
            ach_name=ach_name,
            ach_description=ach_desc,
            base_tags=list(defaults.default_tags or []),
            per_game_tags=per_game_tags,
            offset_seconds=offset,
            episode_number=video.series_number,
            parent_filename=parent_filename,
        )

        clip_video, draft = _upsert_clip_video_and_draft(
            db,
            parent=video,
            folder=folder,
            clip_path=clip_path,
            duration=duration,
            offset=offset,
            achievement=item,
            ach_name=ach_name,
            ach_desc=ach_desc,
            timestamp_label=timestamp_label,
            seo=seo,
            ollama=ollama,
            settings=settings,
        )

        sidecar = {
            "parent_video_id": video.id,
            "clip_video_id": clip_video.id,
            "parent_video_filename": parent_filename,
            "game": folder.name,
            "steam_app_id": folder.steam_app_id,
            "achievement_name": ach_name,
            "achievement_description": ach_desc,
            "offset_seconds": offset,
            "offset_label": timestamp_label,
            "start_seconds": start,
            "duration_seconds": duration,
            "title": draft.title_ptbr,
            "description": draft.description_ptbr,
            "tags": list(draft.tags or []),
        }
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        results.append(
            TrimmedClip(
                filename=clip_path.name,
                clip_path=str(clip_path),
                sidecar_path=str(sidecar_path),
                clip_video_id=clip_video.id,
                parent_video_id=video.id,
                achievement_name=ach_name,
                offset_seconds=offset,
                start_seconds=start,
                duration_seconds=duration,
                title=draft.title_ptbr,
                description=draft.description_ptbr,
                tags=list(draft.tags or []),
            )
        )

    db.commit()
    logger.info(
        "trim_clips_generated",
        extra={"video_id": video.id, "count": len(results), "total": len(timed)},
    )
    return results


def list_clip_metadata(cuts_root: Path) -> list[dict[str, Any]]:
    """Return files in cuts_root as items with paired JSON sidecar metadata."""
    out: list[dict[str, Any]] = []
    if not cuts_root.exists():
        return out
    for p in sorted(cuts_root.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        sidecar = p.with_suffix(".json")
        meta: dict[str, Any] = {}
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        st = p.stat()
        out.append(
            {
                "name": p.name,
                "size_mb": round(st.st_size / (1024 * 1024), 2),
                "mtime": st.st_mtime,
                "meta": meta,
            }
        )
    return out


# ---- internals ---------------------------------------------------------


def _upsert_clip_video_and_draft(
    db: Session,
    *,
    parent: VideoAsset,
    folder: SeriesFolder,
    clip_path: Path,
    duration: int,
    offset: int,
    achievement: dict[str, Any],
    ach_name: str,
    ach_desc: str,
    timestamp_label: str,
    seo: dict[str, Any],
    ollama: OllamaClient,
    settings: Settings,
) -> tuple[VideoAsset, MetadataDraft]:
    full_path = str(clip_path.resolve())

    clip_video = db.execute(
        select(VideoAsset).where(VideoAsset.source_path == full_path)
    ).scalar_one_or_none()

    recorded_at = None
    if parent.recorded_at is not None:
        recorded_at = parent.recorded_at + timedelta(seconds=int(offset))

    clip_session = {
        "is_clip": True,
        "parent_video_id": parent.id,
        "parent_video_filename": parent.filename,
        "achievement_name": ach_name,
        "achievement_description": ach_desc,
        "achievement_offset_seconds": int(offset),
        "achievement_offset_label": timestamp_label,
        "achievements_unlocked": [ach_name],
        "achievements_unlocked_detailed": [achievement],
    }

    if clip_video is None:
        clip_video = VideoAsset(
            folder_id=folder.id,
            filename=clip_path.name,
            source_path=full_path,
            recorded_at=recorded_at,
            duration_sec=int(duration),
            series_number=None,
            thumbnail_prompt=None,
            status=VideoStatus.DRAFT_READY.value,
            language=parent.language or settings.default_language,
            session_payload=clip_session,
            transcript_status=TranscriptStatus.SKIPPED.value,
            chapters=[],
            ollama_status="DONE",
        )
        db.add(clip_video)
        db.flush()
    else:
        clip_video.folder_id = folder.id
        clip_video.filename = clip_path.name
        clip_video.duration_sec = int(duration)
        clip_video.recorded_at = recorded_at or clip_video.recorded_at
        clip_video.status = VideoStatus.DRAFT_READY.value
        clip_video.transcript_status = TranscriptStatus.SKIPPED.value
        clip_video.session_payload = clip_session
        clip_video.ollama_status = "DONE"

    # Deactivate any prior drafts on this clip and create a fresh active one.
    if clip_video.drafts:
        for existing in clip_video.drafts:
            existing.is_active = False
        next_version = max((d.version or 1) for d in clip_video.drafts) + 1
    else:
        next_version = 1

    draft = MetadataDraft(
        video_id=clip_video.id,
        title_ptbr=seo["title"][:200],
        description_ptbr=seo["description"],
        tags=list(seo["tags"] or []),
        chapters=[],
        thumbnail_path=None,
        thumbnail_prompt=None,
        model_provider="ollama" if ollama.is_enabled() else "template",
        model_name=settings.ollama_model if ollama.is_enabled() else None,
        language=parent.language or settings.default_language or "pt-BR",
        version=next_version,
        is_active=True,
    )
    db.add(draft)
    db.flush()
    return clip_video, draft


def _ffmpeg_extract(
    source: Path, output: Path, *, start_seconds: int, duration_seconds: int
) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        _hhmmss(start_seconds),
        "-i",
        str(source),
        "-t",
        str(int(duration_seconds)),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=600)
        if not output.exists() or output.stat().st_size == 0:
            logger.warning(
                "ffmpeg_trim_empty_output",
                extra={"output": str(output), "stderr": (result.stderr or b"").decode("utf-8", "ignore")[-400:]},
            )
            return False
        return True
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "ignore")
        logger.warning(
            "ffmpeg_trim_failed",
            extra={"returncode": exc.returncode, "stderr": stderr[-400:], "cmd": " ".join(command)},
        )
        return False
    except FileNotFoundError as exc:
        logger.warning("ffmpeg_trim_missing_binary", extra={"err": str(exc)})
        return False
    except subprocess.TimeoutExpired as exc:
        logger.warning("ffmpeg_trim_timeout", extra={"err": str(exc)[:200]})
        return False


def _build_seo_content(
    *,
    ollama: OllamaClient,
    game_name: str,
    ach_name: str,
    ach_description: str,
    base_tags: list[str],
    per_game_tags: list[str],
    offset_seconds: int,
    episode_number: int | None,
    parent_filename: str,
) -> dict[str, Any]:
    fallback = _fallback_seo(
        game_name=game_name,
        ach_name=ach_name,
        ach_description=ach_description,
        base_tags=base_tags,
        per_game_tags=per_game_tags,
        offset_seconds=offset_seconds,
        episode_number=episode_number,
    )

    if not ollama.is_enabled():
        return _with_exact_moment(fallback, ach_name, offset_seconds, parent_filename)

    prompt = (
        "Voce e um redator de YouTube em portugues do Brasil especializado em SEO para clipes "
        "de gameplay. Crie metadados para um corte curto que mostra o desbloqueio de uma conquista "
        f"no jogo {game_name}.\n"
        f"Conquista: {ach_name}\n"
        f"Descricao da conquista: {ach_description or 'sem descricao'}\n"
        f"Tags base: {', '.join(base_tags + per_game_tags) or 'nenhuma'}.\n"
        "Regras: titulo com no maximo 95 chars, sem clickbait enganoso, em pt-BR, "
        "incluindo o nome do jogo e da conquista; descricao com 2-3 paragrafos curtos "
        "(mencionar 'conquista' e 'gameplay'); 12 tags pt-BR curtas, sem #, sem virgulas dentro da tag. "
        'Responda em JSON: {"title": "...", "description": "...", "tags": ["...", "..."]}.'
    )

    raw = ollama._call(prompt, temperature=0.5)  # noqa: SLF001
    parsed = OllamaClient._parse_json(raw) if raw else None  # noqa: SLF001
    if not isinstance(parsed, dict):
        return _with_exact_moment(fallback, ach_name, offset_seconds, parent_filename)

    title = str(parsed.get("title") or fallback["title"]).strip()[:100] or fallback["title"]
    description = str(parsed.get("description") or fallback["description"]).strip()
    tags_raw = parsed.get("tags")
    if isinstance(tags_raw, list) and tags_raw:
        tags = _merge_tags(tags_raw, base_tags + per_game_tags + ["conquista", game_name.lower()])
    else:
        tags = fallback["tags"]

    return _with_exact_moment(
        {"title": title, "description": description, "tags": tags},
        ach_name,
        offset_seconds,
        parent_filename,
    )


def _with_exact_moment(
    seo: dict[str, Any], ach_name: str, offset_seconds: int, parent_filename: str
) -> dict[str, Any]:
    """Always append a deterministic footer with the precise original timestamp."""
    timestamp = _hhmmss(offset_seconds)
    footer = (
        "\n\n---\n"
        f"⏱️ Momento exato no video original: {timestamp}\n"
        f"🏆 Conquista: {ach_name}\n"
        f"🎬 Video original: {parent_filename}"
    )
    desc = (seo.get("description") or "").rstrip()
    seo["description"] = f"{desc}{footer}"
    return seo


def _fallback_seo(
    *,
    game_name: str,
    ach_name: str,
    ach_description: str,
    base_tags: list[str],
    per_game_tags: list[str],
    offset_seconds: int,
    episode_number: int | None,
) -> dict[str, Any]:
    ep_label = f" Episodio {episode_number:02d}" if episode_number else ""
    title = f"{game_name} - Conquista: {ach_name}{ep_label} | Gameplay PT-BR"[:100]

    description_lines = [
        f"Corte do gameplay de {game_name} mostrando o desbloqueio da conquista \"{ach_name}\".",
    ]
    if ach_description:
        description_lines.append(f"Descricao da conquista: {ach_description}")
    description_lines += [
        "Gameplay sem comentarios em portugues do Brasil.",
        "",
        "#GameplayPTBR #SemComentarios #Conquistas",
    ]
    description = "\n".join(description_lines)

    seed = [
        game_name.lower(),
        f"{game_name.lower()} conquista",
        f"conquista {ach_name.lower()}",
        ach_name.lower(),
        "conquista",
        "achievement",
        "gameplay",
        "gameplay pt-br",
        "sem comentarios",
        "highlight",
        "clip",
        "ptbr",
    ]
    tags = _merge_tags(seed, base_tags, per_game_tags)
    return {"title": title, "description": description, "tags": tags}


def _per_game_tags(game_name: str) -> list[str]:
    per_game = get_game_tag_defaults() or {}
    folder_key = (game_name or "").strip().lower()
    out: list[str] = []
    for game, values in per_game.items():
        gk = (game or "").strip().lower()
        if gk and (gk == folder_key or gk in folder_key or folder_key in gk):
            out.extend(values)
    return out


def _merge_tags(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for tag in lst or []:
            value = " ".join(str(tag).split()).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
    return out[:15]


def _hhmmss(total_seconds: int) -> str:
    total = max(0, int(total_seconds))
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"
