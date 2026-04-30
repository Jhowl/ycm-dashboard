"""Trim short clips around each Steam achievement unlock and generate
SEO content (title / description / tags) for each clip.

Output layout — clips land inside the same series-folder that already lives
on the host-mounted video volume, so the user can find them right next to
the source mp4:

    <series_folder.path>/cut/
        {video_id_short}__{idx:02d}-{ach-slug}.mp4
        {video_id_short}__{idx:02d}-{ach-slug}.json   # SEO sidecar
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from slugify import slugify
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import SeriesFolder, VideoAsset
from app.services.channel import get_or_create_channel_defaults
from app.services.errors import NotFoundError, ValidationError
from app.services.game_defaults import get_game_tag_defaults
from app.services.ollama import OllamaClient

logger = get_logger(__name__)


@dataclass(slots=True)
class TrimmedClip:
    filename: str
    clip_path: str
    sidecar_path: str
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
    pre_seconds: int = 15,
    post_seconds: int = 15,
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

    defaults = get_or_create_channel_defaults(db, settings)
    per_game_tags = _per_game_tags(folder.name)
    ollama = OllamaClient(settings)

    cuts_root = Path(folder.path) / "cut"
    cuts_root.mkdir(parents=True, exist_ok=True)

    duration = max(1, int(pre_seconds) + int(post_seconds))
    sorted_items = sorted(timed, key=lambda i: int(i.get("offset_seconds") or 0))

    short_id = video.id[:8]
    results: list[TrimmedClip] = []
    for idx, item in enumerate(sorted_items, start=1):
        offset = max(0, int(item.get("offset_seconds") or 0))
        start = max(0, offset - int(pre_seconds))
        ach_name = str(
            item.get("displayName") or item.get("name") or item.get("apiName") or "achievement"
        ).strip()
        ach_desc = str(item.get("description") or "").strip()

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
        )

        sidecar = {
            "video_id": video.id,
            "video_filename": video.filename,
            "game": folder.name,
            "steam_app_id": folder.steam_app_id,
            "achievement_name": ach_name,
            "achievement_description": ach_desc,
            "offset_seconds": offset,
            "start_seconds": start,
            "duration_seconds": duration,
            "title": seo["title"],
            "description": seo["description"],
            "tags": seo["tags"],
        }
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        results.append(
            TrimmedClip(
                filename=clip_path.name,
                clip_path=str(clip_path),
                sidecar_path=str(sidecar_path),
                achievement_name=ach_name,
                offset_seconds=offset,
                start_seconds=start,
                duration_seconds=duration,
                title=seo["title"],
                description=seo["description"],
                tags=seo["tags"],
            )
        )

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
        return fallback

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
        return fallback

    title = str(parsed.get("title") or fallback["title"]).strip()[:100] or fallback["title"]
    description = str(parsed.get("description") or fallback["description"]).strip()
    tags_raw = parsed.get("tags")
    if isinstance(tags_raw, list) and tags_raw:
        tags = _merge_tags(tags_raw, base_tags + per_game_tags + ["conquista", game_name.lower()])
    else:
        tags = fallback["tags"]

    return {"title": title, "description": description, "tags": tags}


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

    timestamp = _hhmmss(offset_seconds)
    description_lines = [
        f"Corte do gameplay de {game_name} mostrando o desbloqueio da conquista \"{ach_name}\".",
    ]
    if ach_description:
        description_lines.append(f"Descricao da conquista: {ach_description}")
    description_lines += [
        f"Momento original do video: {timestamp}.",
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
