from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: str
    title_ptbr: str
    description_ptbr: str
    tags: list[str]
    thumbnail_path: str | None
    model_provider: str
    language: str
    version: int
    is_active: bool
    created_at: datetime


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    folder_id: str
    filename: str
    source_path: str
    recorded_at: datetime | None
    duration_sec: int | None
    series_number: int | None
    thumbnail_prompt: str | None
    status: str
    language: str
    uploaded_url: str | None
    created_at: datetime
    latest_draft: DraftOut | None = None


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    path: str
    series_url: str
    steam_app_id: int | None
    steam_game_name: str | None
    active: bool
    video_count: int
    last_scan_at: datetime | None


class FolderDetailOut(FolderOut):
    videos: list[VideoOut]


class ChannelDefaultsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_name: str
    language: str
    default_tags: list[str]
    pc_config: str
    default_description_block: str
    default_visibility: str
    updated_at: datetime


class ChannelDefaultsPatch(BaseModel):
    channel_name: str | None = None
    language: str | None = None
    default_tags: list[str] | None = None
    pc_config: str | None = None
    default_description_block: str | None = None
    default_visibility: str | None = Field(default=None, pattern="^(private|unlisted|public)$")


class ScanResultOut(BaseModel):
    discovered_folders: int
    new_folders: int
    reactivated_folders: int
    deactivated_folders: int
    new_videos: int
    scanned_at: datetime


class SeriesDetailOut(BaseModel):
    folder: FolderOut
    videos: list[VideoOut]


class FolderUrlOut(BaseModel):
    folder_id: str
    slug: str
    series_url: str


class FolderSteamLinkPatch(BaseModel):
    steam_app_id: int | None = None
    steam_game_name: str | None = None


class TelegramWebhookIn(BaseModel):
    message: dict | None = None


class JobActionOut(BaseModel):
    ok: bool
    message: str


class VideoGenerateOut(BaseModel):
    ok: bool
    video_id: str
    draft_id: str


class VideoSettingsPatch(BaseModel):
    series_number: int | None = Field(default=None, ge=1)
    thumbnail_prompt: str | None = None


class HomeStatsOut(BaseModel):
    folders_total: int
    folders_active: int
    pending_drafts: int
    ready_to_upload: int


class ScanRequest(BaseModel):
    root_path: str | None = None
