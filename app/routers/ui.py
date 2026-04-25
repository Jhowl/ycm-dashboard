from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import SeriesFolder, TranscriptStatus, VideoAsset
from app.services.channel import get_or_create_channel_defaults, update_channel_defaults_from_form
from app.services.dashboard import build_home_stats, get_youtube_token_status
from app.services.folders import sync_folders_and_videos, update_folder_steam_link
from app.services.game_defaults import game_tag_defaults_text, save_game_tag_defaults
from app.services.metadata import (
    approve_video,
    generate_metadata_draft,
    reject_video,
    update_video_settings,
    get_latest_draft,
)
from app.services.ollama import OllamaClient
from app.services.serialization import video_to_schema
from app.services.steam import (
    get_steam_dashboard_data,
    get_steam_owned_games,
    get_steam_recent_games,
)
from app.services.steam_news import fetch_steam_news
from app.services.steam_screenshots import fetch_steam_screenshots
from app.services.thumbnail_lab import thumbnail_lab_dir
from app.services.transcription import load_transcript
from app.services.youtube_oauth import (
    build_youtube_auth_url,
    exchange_code_for_tokens,
    generate_oauth_state,
    save_token_payload,
)
from app.time_utils import format_datetime_ny
from worker.tasks import upload_video_task, generate_thumbnail_options_task
from worker.celery_app import celery_app

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["ny_datetime"] = format_datetime_ny


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    defaults = get_or_create_channel_defaults(db, settings)
    youtube_token_exists, youtube_token_updated_at = get_youtube_token_status(settings.youtube_token_file)
    steam_data = get_steam_dashboard_data(settings)
    folders = db.execute(select(SeriesFolder).order_by(SeriesFolder.name.asc())).scalars().all()

    return templates.TemplateResponse(
        name="home.html",
        request=request,
        context={
            "defaults": defaults,
            "stats": build_home_stats(db),
            "folders": folders,
            "video_root": settings.video_root,
            "youtube_client_id_configured": bool(settings.youtube_client_id),
            "youtube_redirect_uri": settings.youtube_redirect_uri,
            "youtube_token_file": settings.youtube_token_file,
            "youtube_token_exists": youtube_token_exists,
            "youtube_token_updated_at": youtube_token_updated_at,
            "steam_data": steam_data,
            "ollama_model": settings.ollama_model,
            "ollama_enabled": settings.ollama_enabled,
            "whisper_enabled": settings.whisper_enabled,
        },
    )


@router.get("/config")
def config_page(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    settings = apply_runtime_overrides(settings, db)
    defaults = get_or_create_channel_defaults(db, settings)
    youtube_token_exists, youtube_token_updated_at = get_youtube_token_status(settings.youtube_token_file)
    ollama_status = OllamaClient(settings).health()

    return templates.TemplateResponse(
        name="config.html",
        request=request,
        context={
            "defaults": defaults,
            "youtube_client_id_configured": bool(settings.youtube_client_id),
            "youtube_redirect_uri": settings.youtube_redirect_uri,
            "youtube_token_file": settings.youtube_token_file,
            "youtube_token_exists": youtube_token_exists,
            "youtube_token_updated_at": youtube_token_updated_at,
            "game_tag_defaults_json": game_tag_defaults_text(),
            "ollama_base_url": settings.ollama_base_url,
            "ollama_model": settings.ollama_model,
            "ollama_status": ollama_status,
            "whisper_enabled": settings.whisper_enabled,
            "whisper_model": settings.whisper_model,
            "whisper_device": settings.whisper_device,
            "transcripts_root": settings.transcripts_root,
        },
    )


@router.post("/ui/channel-defaults")
def update_channel_defaults(
    request: Request,
    channel_name: str = Form(...),
    language: str = Form(...),
    default_tags: str = Form(...),
    pc_config: str = Form(...),
    default_description_block: str = Form(...),
    default_visibility: str = Form(...),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    defaults = get_or_create_channel_defaults(db, settings)
    update_channel_defaults_from_form(
        defaults,
        channel_name=channel_name,
        language=language,
        default_tags=default_tags,
        pc_config=pc_config,
        default_description_block=default_description_block,
        default_visibility=default_visibility,
    )

    db.commit()

    target = request.headers.get("Referer") or "/config"
    return RedirectResponse(url=target, status_code=303)


@router.post("/ui/game-tag-defaults")
def update_game_tag_defaults(
    request: Request,
    game_tag_defaults_json: str = Form(...),
):
    import json

    try:
        payload = json.loads(game_tag_defaults_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON invalido: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Formato invalido: use objeto JSON {\"Jogo\": [\"tag1\", ...]}")

    normalized: dict[str, list[str]] = {}
    for game, tags in payload.items():
        if not isinstance(game, str) or not game.strip():
            continue
        if not isinstance(tags, list):
            continue
        values = [str(tag).strip() for tag in tags if str(tag).strip()]
        if values:
            normalized[game.strip()] = values

    save_game_tag_defaults(normalized)
    return _redirect_back(request, fallback="/config")


@router.get("/folders")
def folders_page(request: Request, include_inactive: bool = False, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    stmt = select(SeriesFolder).order_by(SeriesFolder.name.asc())
    if not include_inactive:
        stmt = stmt.where(SeriesFolder.active.is_(True))

    folders = db.execute(stmt).scalars().all()
    steam_games = get_steam_recent_games(settings, count=40)
    return templates.TemplateResponse(
        name="folders.html",
        request=request,
        context={
            "folders": folders,
            "include_inactive": include_inactive,
            "steam_games": steam_games,
        },
    )


@router.post("/ui/folders/scan")
def scan_folders_ui(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    steam_games = get_steam_recent_games(settings, count=40)
    owned_games = get_steam_owned_games(settings)
    sync_folders_and_videos(
        db, settings, steam_games=steam_games, owned_games=owned_games
    )
    return RedirectResponse(url="/folders", status_code=303)


@router.post("/ui/folders/{folder_id}/steam-link")
def update_folder_steam_link_ui(
    folder_id: str,
    request: Request,
    steam_app_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings

    if not steam_app_id:
        target_app_id = None
        target_game_name = None
    else:
        try:
            target_app_id = int(steam_app_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="steam_app_id invalido") from exc

        steam_games = get_steam_recent_games(settings, count=60)
        game = next((item for item in steam_games if int(item.get("appid", 0)) == target_app_id), None)
        target_game_name = game.get("name") if game else f"App {target_app_id}"

    try:
        update_folder_steam_link(db, folder_id, target_app_id, target_game_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _redirect_back(request, fallback="/folders")


@router.get("/series/{slug}")
def series_page(slug: str, request: Request, db: Session = Depends(get_db)):
    folder = db.execute(
        select(SeriesFolder)
        .where(SeriesFolder.slug == slug)
        .options(selectinload(SeriesFolder.videos).selectinload(VideoAsset.drafts))
    ).scalar_one_or_none()

    if not folder:
        raise HTTPException(status_code=404, detail="Series not found")

    videos = sorted(folder.videos, key=lambda video: video.created_at, reverse=True)
    video_cards = []
    for video in videos:
        schema = video_to_schema(video)
        payload = video.session_payload or {}
        task_id = payload.get("upload_task_id")
        task_status = None
        if isinstance(task_id, str) and task_id:
            result = AsyncResult(task_id, app=celery_app)
            task_status = (result.status or "").upper() or None

        card = schema.model_dump()
        card["upload_task_id"] = task_id
        card["upload_task_status"] = task_status or payload.get("upload_task_status")
        card["upload_task_error"] = payload.get("upload_task_error")
        video_cards.append(card)

    steam_news = fetch_steam_news(folder.steam_app_id, count=5) if folder.steam_app_id else []

    return templates.TemplateResponse(
        name="series.html",
        request=request,
        context={
            "folder": folder,
            "videos": video_cards,
            "steam_news": steam_news,
            "notice": request.query_params.get("notice"),
        },
    )


@router.get("/ui/partials/series/{slug}/news")
def series_news_partial(slug: str, request: Request, db: Session = Depends(get_db)):
    folder = db.execute(
        select(SeriesFolder).where(SeriesFolder.slug == slug)
    ).scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Series not found")

    steam_news = fetch_steam_news(folder.steam_app_id, count=8) if folder.steam_app_id else []
    return templates.TemplateResponse(
        name="partials/steam_news.html",
        request=request,
        context={"folder": folder, "steam_news": steam_news, "include_wrapper": True},
    )


@router.get("/ui/partials/videos/{video_id}/transcript-status")
def transcript_status_partial(video_id: str, request: Request, db: Session = Depends(get_db)):
    video = db.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return templates.TemplateResponse(
        name="partials/transcript_status.html",
        request=request,
        context={"video": video_to_schema(video)},
    )


def _redirect_back(request: Request, fallback: str = "/folders", notice: str | None = None) -> RedirectResponse:
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    target = request.headers.get("Referer") or fallback
    if notice:
        parsed = urlparse(target)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["notice"] = notice
        target = urlunparse(parsed._replace(query=urlencode(query)))
    return RedirectResponse(url=target, status_code=303)


@router.get("/ui/thumbnail-lab/{video_id}")
def thumbnail_lab_legacy(video_id: str):
    return RedirectResponse(url=f"/ui/video-settings/{video_id}", status_code=302)


@router.get("/ui/video-settings/{video_id}")
def video_settings_page(video_id: str, request: Request, db: Session = Depends(get_db)):
    video = db.execute(
        select(VideoAsset).where(VideoAsset.id == video_id).options(selectinload(VideoAsset.drafts))
    ).scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    out_dir = thumbnail_lab_dir(video.id)
    names = sorted([p.name for p in out_dir.glob("option_*.jpg")]) if out_dir.exists() else []
    options = []
    for idx, name in enumerate(names, start=1):
        asset_path = thumbnail_lab_dir(video.id) / name
        version = int(asset_path.stat().st_mtime) if asset_path.exists() else 0
        options.append(
            {
                "label": f"Opção {idx}",
                "image_url": f"/ui/video-settings/{video.id}/asset/{name}?v={version}",
                "download_url": f"/ui/video-settings/{video.id}/asset/{name}?download=1&v={version}",
                "prompt": (
                    f"Use esta imagem como base para thumbnail de YouTube do episódio {video.series_number or 'X'} de Resident Evil Requiem. "
                    f"Adicionar texto grande e legível: EP {video.series_number or 'X'} e subtítulo curto de alto CTR. "
                    "Manter estilo cinematográfico, alto contraste, iluminação dramática e visual limpo em 16:9."
                ),
            }
        )

    folder = db.get(SeriesFolder, video.folder_id)
    settings = request.app.state.settings
    steam_images = fetch_steam_screenshots(settings.steam_id, folder.steam_app_id if folder else None, limit=20)

    episode_links = []
    if folder:
        siblings = db.execute(
            select(VideoAsset).where(VideoAsset.folder_id == folder.id).order_by(VideoAsset.series_number.asc(), VideoAsset.created_at.asc())
        ).scalars().all()
        for sib in siblings:
            ep = sib.series_number or 0
            episode_links.append(
                {
                    "label": f"EP {ep:02d}" if ep else sib.filename,
                    "href": f"/ui/video-settings/{sib.id}",
                    "active": sib.id == video.id,
                }
            )

    latest = get_latest_draft(video)
    transcript = load_transcript(video)
    return templates.TemplateResponse(
        name="thumbnail_lab.html",
        request=request,
        context={
            "video": video_to_schema(video),
            "latest_draft": latest,
            "options": options,
            "steam_images": steam_images,
            "images_ready": bool(options),
            "episode_links": episode_links,
            "transcript": transcript,
        },
    )


@router.post("/ui/video-settings/{video_id}/generate-images")
def video_settings_generate_images(video_id: str, request: Request, db: Session = Depends(get_db)):
    video = db.get(VideoAsset, video_id)
    if not video:
        return _redirect_back(request, notice="erro:Video not found")

    task = generate_thumbnail_options_task.delay(video_id)
    short_task = task.id[:8] if task.id else "sem-id"
    return RedirectResponse(url=f"/ui/video-settings/{video_id}?notice=images_fila:{short_task}", status_code=303)


@router.get("/cuts")
def cuts_page(request: Request):
    settings = request.app.state.settings
    cuts_dir = Path(settings.cuts_root)
    items = []
    if cuts_dir.exists():
        for p in sorted(cuts_dir.glob('*.mp4'), key=lambda x: x.stat().st_mtime, reverse=True):
            st = p.stat()
            items.append({
                'name': p.name,
                'size_mb': round(st.st_size / (1024 * 1024), 2),
                'mtime': format_datetime_ny(datetime.fromtimestamp(st.st_mtime, timezone.utc)),
            })
    return templates.TemplateResponse(
        name='cuts.html',
        request=request,
        context={'cuts': items, 'cuts_dir': str(cuts_dir)},
    )


@router.get('/ui/cuts/file/{filename}')
def cuts_file(filename: str, request: Request, download: int = 0):
    settings = request.app.state.settings
    safe = Path(filename).name
    path = Path(settings.cuts_root) / safe
    if not path.exists() or path.suffix.lower() != '.mp4':
        raise HTTPException(status_code=404, detail='File not found')
    return FileResponse(path, filename=safe if download else None)


@router.get("/ui/video-settings/{video_id}/asset/{filename}")
def thumbnail_lab_asset(video_id: str, filename: str, download: int = 0, v: int = 0):
    valid = {f"option_{i}.jpg" for i in range(1, 21)}
    if filename not in valid:
        raise HTTPException(status_code=404, detail="Asset not found")

    path = thumbnail_lab_dir(video_id) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset not found")

    return FileResponse(
        path,
        filename=filename if download else None,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.post("/ui/videos/{video_id}/generate")
def generate_video_ui(video_id: str, request: Request, db: Session = Depends(get_db)):
    """Enqueue metadata generation in the background.

    Sets ollama_status=PENDING immediately so the UI chip starts polling.
    The Celery task picks the latest runtime overrides via worker_session().
    """
    video = db.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    from app.services import ollama_progress as _progress
    _progress.reset(db, video_id)

    try:
        from worker.tasks import generate_metadata_task
        generate_metadata_task.delay(video_id)
    except Exception as exc:
        # Fall back to synchronous if broker unreachable
        settings = request.app.state.settings
        settings = apply_runtime_overrides(settings, db)
        try:
            generate_metadata_draft(db, settings, video_id)
        except ValueError as inner:
            raise HTTPException(status_code=404, detail=str(inner)) from inner
        return _redirect_back(request, notice="draft_gerado_sync")
    return _redirect_back(request, notice="draft_enfileirado")


@router.post("/ui/videos/{video_id}/transcribe")
def trigger_transcription_ui(video_id: str, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    video = db.get(VideoAsset, video_id)
    if not video:
        return _redirect_back(request, notice="erro:Video nao encontrado")
    if not settings.whisper_enabled:
        return _redirect_back(request, notice="erro:Whisper desativado")

    try:
        from worker.tasks import transcribe_video_task

        task = transcribe_video_task.delay(video_id)
        short_task = task.id[:8] if task.id else "sem-id"
    except Exception as exc:  # noqa: BLE001
        return _redirect_back(request, notice=f"erro:celery {str(exc)[:40]}")

    video.transcript_status = TranscriptStatus.PENDING.value
    video.transcript_error = None
    db.commit()
    return _redirect_back(request, notice=f"transcricao_fila:{short_task}")


@router.post("/ui/videos/{video_id}/settings")
def update_video_settings_ui(
    video_id: str,
    request: Request,
    series_number: int | None = Form(default=None),
    thumbnail_prompt: str = Form(default=""),
    db: Session = Depends(get_db),
):
    try:
        update_video_settings(db, video_id, series_number, thumbnail_prompt)
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc
    return _redirect_back(request)


@router.post("/ui/videos/{video_id}/approve")
def approve_video_ui(video_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        approve_video(db, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _redirect_back(request, notice="video_aprovado")


@router.post("/ui/videos/{video_id}/reject")
def reject_video_ui(video_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        reject_video(db, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _redirect_back(request, notice="video_rejeitado")


@router.post("/ui/videos/{video_id}/upload")
def upload_video_ui(video_id: str, request: Request, db: Session = Depends(get_db)):
    video = db.get(VideoAsset, video_id)
    if not video:
        return _redirect_back(request, notice="erro:Video not found")
    if video.status != "APPROVED":
        return _redirect_back(request, notice="erro:Aprove o video antes do upload")

    task = upload_video_task.delay(video_id)
    if task.id:
        payload = dict(video.session_payload or {})
        payload["upload_task_id"] = task.id
        payload["upload_task_status"] = "PENDING"
        video.session_payload = payload
        db.commit()

    short_task = task.id[:8] if task.id else "sem-id"
    return _redirect_back(request, notice=f"upload_fila:{short_task}")


@router.get("/ui/youtube/oauth/start")
def youtube_oauth_start(request: Request):
    settings = request.app.state.settings
    state = generate_oauth_state()
    try:
        auth_url = build_youtube_auth_url(settings, state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        "youtube_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/ui/youtube/oauth/callback")
def youtube_oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    expected_state = request.cookies.get("youtube_oauth_state")

    if error:
        return templates.TemplateResponse(
            name="youtube_oauth_result.html",
            request=request,
            context={
                "success": False,
                "message": f"OAuth returned error: {error}",
                "token_path": None,
            },
            status_code=400,
        )

    if not state or not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    settings = request.app.state.settings
    try:
        token_payload = exchange_code_for_tokens(settings, code)
        token_path = save_token_payload(settings, token_payload)
        message = "YouTube token generated and saved successfully."
        success = True
    except (ValueError, RuntimeError, OSError) as exc:
        token_path = None
        message = str(exc)
        success = False

    response = templates.TemplateResponse(
        name="youtube_oauth_result.html",
        request=request,
        context={
            "success": success,
            "message": message,
            "token_path": str(token_path) if token_path else None,
        },
        status_code=200 if success else 500,
    )
    response.delete_cookie("youtube_oauth_state")
    return response


# ================================================================
# v0.3 — new pages: video detail, jobs, settings, series listing.
# ================================================================
from app.models import MetadataDraft  # noqa: E402
from app.services.jobs import collect_jobs  # noqa: E402
from app.services.runtime_settings import (  # noqa: E402
    apply_runtime_overrides,
    get_runtime_ai,
    update_runtime_ai,
)


def _episode_links_for_folder(db: Session, folder: SeriesFolder, current_id: str | None) -> list[dict]:
    siblings = db.execute(
        select(VideoAsset)
        .where(VideoAsset.folder_id == folder.id)
        .order_by(VideoAsset.series_number.asc(), VideoAsset.created_at.asc())
    ).scalars().all()
    links: list[dict] = []
    for sib in siblings:
        ep = sib.series_number or 0
        links.append({
            "label": f"EP {ep:02d}" if ep else sib.filename,
            "href": f"/videos/{sib.id}",
            "active": sib.id == current_id,
        })
    return links


@router.get("/videos/{video_id}")
def video_detail_page(video_id: str, request: Request, db: Session = Depends(get_db)):
    video = db.execute(
        select(VideoAsset).where(VideoAsset.id == video_id).options(selectinload(VideoAsset.drafts))
    ).scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    folder = db.get(SeriesFolder, video.folder_id)

    latest = get_latest_draft(video)
    transcript = load_transcript(video)
    session_payload = video.session_payload or {}
    achievements_unlocked = session_payload.get('achievements_unlocked') or []
    achievements_unlocked_detailed = session_payload.get('achievements_unlocked_detailed') or []

    out_dir = thumbnail_lab_dir(video.id)
    thumb_files: list[dict] = []
    if out_dir.exists():
        for p in sorted(out_dir.glob("option_*.jpg"))[:4]:
            thumb_files.append({"url": f"/ui/video-settings/{video.id}/asset/{p.name}?v={int(p.stat().st_mtime)}"})

    return templates.TemplateResponse(
        name="video_detail.html",
        request=request,
        context={
            "video": video,
            "folder": folder,
            "latest_draft": latest,
            "transcript": transcript,
            "thumb_files": thumb_files,
            "achievements_unlocked": achievements_unlocked,
            "achievements_unlocked_detailed": achievements_unlocked_detailed,
            "episode_links": _episode_links_for_folder(db, folder, video.id) if folder else [],
        },
    )


@router.post("/ui/videos/{video_id}/draft")
def save_draft_ui(
    video_id: str,
    request: Request,
    title_ptbr: str = Form(default=""),
    description_ptbr: str = Form(default=""),
    tags: str = Form(default=""),
    db: Session = Depends(get_db),
):
    draft = db.execute(
        select(MetadataDraft)
        .where(MetadataDraft.video_id == video_id, MetadataDraft.is_active.is_(True))
        .order_by(MetadataDraft.version.desc())
    ).scalars().first()
    if not draft:
        raise HTTPException(status_code=404, detail="No active draft")

    draft.title_ptbr = title_ptbr.strip() or draft.title_ptbr
    draft.description_ptbr = description_ptbr
    if tags is not None:
        draft.tags = [t.strip() for t in tags.split(",") if t.strip()]
    db.commit()
    return {"ok": True}


@router.get("/series", include_in_schema=False)
def series_list_page(request: Request, db: Session = Depends(get_db)):
    folders = db.execute(
        select(SeriesFolder).order_by(SeriesFolder.active.desc(), SeriesFolder.name.asc())
    ).scalars().all()
    return templates.TemplateResponse(
        name="series_list.html",
        request=request,
        context={"folders": folders},
    )


@router.get("/jobs", include_in_schema=False)
def jobs_page(request: Request):
    return templates.TemplateResponse(name="jobs.html", request=request, context={})


@router.get("/ui/partials/jobs-summary")
def jobs_summary_partial(request: Request):
    payload = collect_jobs(celery_app)
    return templates.TemplateResponse(
        name="partials/jobs_summary.html",
        request=request,
        context=payload,
    )


@router.get("/ui/partials/jobs-list")
def jobs_list_partial(request: Request):
    payload = collect_jobs(celery_app)
    return templates.TemplateResponse(
        name="partials/jobs_list.html",
        request=request,
        context=payload,
    )


@router.get("/ui/partials/ollama-pulse")
def ollama_pulse_partial(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    settings = apply_runtime_overrides(settings, db)
    runtime = get_runtime_ai(db, settings)
    eff = settings.model_copy(update={"ollama_model": runtime.ollama_model})
    health = OllamaClient(eff).health()
    return templates.TemplateResponse(
        name="partials/ollama_pulse.html",
        request=request,
        context={
            "ok": bool(health.get("ok")),
            "model": runtime.ollama_model,
            "reason": health.get("reason"),
        },
    )


@router.get("/settings", include_in_schema=False)
def settings_page(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    settings = apply_runtime_overrides(settings, db)
    runtime = get_runtime_ai(db, settings)
    defaults = get_or_create_channel_defaults(db, settings)
    youtube_token_exists, youtube_token_updated_at = get_youtube_token_status(settings.youtube_token_file)
    eff = settings.model_copy(update={"ollama_model": runtime.ollama_model})
    health = OllamaClient(eff).health()
    return templates.TemplateResponse(
        name="settings.html",
        request=request,
        context={
            "defaults": defaults,
            "settings": settings,
            "runtime": runtime,
            "ollama_models": list(health.get("models") or []),
            "ollama_ok": bool(health.get("ok")),
            "ollama_reason": health.get("reason"),
            "youtube_client_id_configured": bool(settings.youtube_client_id),
            "youtube_redirect_uri": settings.youtube_redirect_uri,
            "youtube_token_file": settings.youtube_token_file,
            "youtube_token_exists": youtube_token_exists,
            "youtube_token_updated_at": youtube_token_updated_at,
        },
    )


@router.post("/ui/settings/runtime-ai")
def settings_update_runtime_ai(
    request: Request,
    ollama_model: str = Form(default=""),
    ollama_enabled: str | None = Form(default=None),
    whisper_model: str = Form(default=""),
    whisper_enabled: str | None = Form(default=None),
    whisper_auto_run: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    update_runtime_ai(
        db,
        ollama_model=ollama_model.strip() or None,
        ollama_enabled=bool(ollama_enabled),
        whisper_model=whisper_model.strip() or None,
        whisper_enabled=bool(whisper_enabled),
        whisper_auto_run=bool(whisper_auto_run),
    )
    return _redirect_back(request, fallback="/settings", notice="settings_saved")


@router.get("/ui/partials/ollama-models")
def ollama_models_partial(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    settings = apply_runtime_overrides(settings, db)
    runtime = get_runtime_ai(db, settings)
    eff = settings.model_copy(update={"ollama_model": runtime.ollama_model})
    health = OllamaClient(eff).health()
    return templates.TemplateResponse(
        name="partials/ollama_models.html",
        request=request,
        context={
            "ok": bool(health.get("ok")),
            "models": list(health.get("models") or []),
            "current": runtime.ollama_model,
            "reason": health.get("reason"),
        },
    )


@router.get("/ui/partials/videos/{video_id}/ollama-status")
def ollama_status_partial(video_id: str, request: Request, db: Session = Depends(get_db)):
    video = db.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return templates.TemplateResponse(
        name="partials/ollama_status.html",
        request=request,
        context={"video": video_to_schema(video)},
    )
