from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.dependencies import require_api_token
from app.db import get_db
from app.models import ChannelDefaults, SeriesFolder, VideoAsset
from app.schemas import (
    ChannelDefaultsOut,
    ChannelDefaultsPatch,
    FolderDetailOut,
    FolderOut,
    FolderSteamLinkPatch,
    FolderUrlOut,
    HomeStatsOut,
    JobActionOut,
    ScanRequest,
    ScanResultOut,
    SeriesDetailOut,
    TelegramWebhookIn,
    VideoGenerateOut,
    VideoOut,
    VideoSettingsPatch,
)
from app.services.folders import ensure_channel_defaults, sync_folders_and_videos, update_folder_steam_link
from app.services.metadata import (
    approve_video,
    generate_metadata_draft,
    reject_video,
    update_video_settings,
    upload_video,
)
from app.services.serialization import video_to_schema
from app.services.steam import get_steam_recent_games
from app.services.telegram import handle_telegram_command

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_token)])


@router.post("/folders/scan", response_model=ScanResultOut)
def scan_folders(payload: ScanRequest, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    ensure_channel_defaults(db, settings)
    steam_games = get_steam_recent_games(settings, count=40)
    stats = sync_folders_and_videos(db, settings, payload.root_path, steam_games=steam_games)
    return ScanResultOut(**stats)


@router.get("/folders", response_model=list[FolderOut])
def list_folders(include_inactive: bool = False, db: Session = Depends(get_db)):
    stmt = select(SeriesFolder).order_by(SeriesFolder.name.asc())
    if not include_inactive:
        stmt = stmt.where(SeriesFolder.active.is_(True))
    folders = db.execute(stmt).scalars().all()
    return [FolderOut.model_validate(folder) for folder in folders]


@router.get("/folders/{folder_id}", response_model=FolderDetailOut)
def get_folder(folder_id: str, db: Session = Depends(get_db)):
    folder = db.execute(
        select(SeriesFolder)
        .where(SeriesFolder.id == folder_id)
        .options(selectinload(SeriesFolder.videos).selectinload(VideoAsset.drafts))
    ).scalar_one_or_none()

    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    videos = sorted(folder.videos, key=lambda v: v.created_at, reverse=True)
    return FolderDetailOut(
        **FolderOut.model_validate(folder).model_dump(),
        videos=[video_to_schema(video) for video in videos],
    )


@router.get("/folders/{folder_id}/url", response_model=FolderUrlOut)
def get_folder_url(folder_id: str, db: Session = Depends(get_db)):
    folder = db.get(SeriesFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    return FolderUrlOut(folder_id=folder.id, slug=folder.slug, series_url=folder.series_url)


@router.patch("/folders/{folder_id}/steam-link", response_model=FolderOut)
def patch_folder_steam_link(folder_id: str, payload: FolderSteamLinkPatch, db: Session = Depends(get_db)):
    try:
        folder = update_folder_steam_link(
            db,
            folder_id=folder_id,
            steam_app_id=payload.steam_app_id,
            steam_game_name=payload.steam_game_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return FolderOut.model_validate(folder)


@router.get("/channel/defaults", response_model=ChannelDefaultsOut)
def get_channel_defaults(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    defaults = ensure_channel_defaults(db, settings)
    return ChannelDefaultsOut.model_validate(defaults)


@router.patch("/channel/defaults", response_model=ChannelDefaultsOut)
def patch_channel_defaults(
    payload: ChannelDefaultsPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    defaults = ensure_channel_defaults(db, settings)

    updates = payload.model_dump(exclude_none=True)
    for key, value in updates.items():
        setattr(defaults, key, value)
    defaults.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(defaults)
    return ChannelDefaultsOut.model_validate(defaults)


@router.get("/series/{slug}", response_model=SeriesDetailOut)
def get_series_by_slug(slug: str, db: Session = Depends(get_db)):
    folder = db.execute(
        select(SeriesFolder)
        .where(SeriesFolder.slug == slug)
        .options(selectinload(SeriesFolder.videos).selectinload(VideoAsset.drafts))
    ).scalar_one_or_none()

    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Series not found")

    videos = sorted(folder.videos, key=lambda v: v.created_at, reverse=True)
    return SeriesDetailOut(
        folder=FolderOut.model_validate(folder),
        videos=[video_to_schema(video) for video in videos],
    )


@router.get("/videos/{video_id}", response_model=VideoOut)
def get_video(video_id: str, db: Session = Depends(get_db)):
    video = db.execute(
        select(VideoAsset).where(VideoAsset.id == video_id).options(selectinload(VideoAsset.drafts))
    ).scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    return video_to_schema(video)


@router.post("/videos/{video_id}/generate", response_model=VideoGenerateOut)
def generate_video_metadata(video_id: str, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    try:
        draft = generate_metadata_draft(db, settings, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return VideoGenerateOut(ok=True, video_id=video_id, draft_id=draft.id)


@router.patch("/videos/{video_id}/settings", response_model=VideoOut)
def patch_video_settings(video_id: str, payload: VideoSettingsPatch, db: Session = Depends(get_db)):
    try:
        video = update_video_settings(
            db,
            video_id=video_id,
            series_number=payload.series_number,
            thumbnail_prompt=payload.thumbnail_prompt,
        )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from exc

    return video_to_schema(video)


@router.post("/videos/{video_id}/approve", response_model=JobActionOut)
def approve_video_endpoint(video_id: str, db: Session = Depends(get_db)):
    try:
        approve_video(db, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return JobActionOut(ok=True, message=f"Video {video_id} aprovado")


@router.post("/videos/{video_id}/reject", response_model=JobActionOut)
def reject_video_endpoint(video_id: str, db: Session = Depends(get_db)):
    try:
        reject_video(db, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return JobActionOut(ok=True, message=f"Video {video_id} retornou para INGESTED")


@router.post("/videos/{video_id}/upload", response_model=JobActionOut)
def upload_video_endpoint(video_id: str, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    try:
        uploaded = upload_video(db, settings, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return JobActionOut(ok=True, message=f"Upload concluido: {uploaded.uploaded_url}")


@router.post("/telegram/webhook", response_model=JobActionOut)
def telegram_webhook(payload: TelegramWebhookIn, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    if settings.telegram_webhook_secret:
        incoming = request.headers.get("X-Telegram-Secret")
        if incoming != settings.telegram_webhook_secret:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram secret")

    text = ""
    if payload.message:
        text = str(payload.message.get("text") or "")

    try:
        reply = handle_telegram_command(db, text, settings)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return JobActionOut(ok=True, message=reply)


@router.get("/home/stats", response_model=HomeStatsOut)
def home_stats(db: Session = Depends(get_db)):
    folders_total = db.execute(select(func.count(SeriesFolder.id))).scalar_one()
    folders_active = db.execute(
        select(func.count(SeriesFolder.id)).where(SeriesFolder.active.is_(True))
    ).scalar_one()
    pending_drafts = db.execute(
        select(func.count(VideoAsset.id)).where(VideoAsset.status.in_(["INGESTED", "DRAFT_READY"]))
    ).scalar_one()
    ready_to_upload = db.execute(
        select(func.count(VideoAsset.id)).where(VideoAsset.status == "APPROVED")
    ).scalar_one()

    return HomeStatsOut(
        folders_total=folders_total,
        folders_active=folders_active,
        pending_drafts=pending_drafts,
        ready_to_upload=ready_to_upload,
    )
