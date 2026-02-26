from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class VideoStatus(StrEnum):
    INGESTED = "INGESTED"
    DRAFT_READY = "DRAFT_READY"
    APPROVED = "APPROVED"
    UPLOADED = "UPLOADED"
    FAILED = "FAILED"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class SeriesFolder(TimestampMixin, Base):
    __tablename__ = "series_folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    series_url: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    steam_app_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    steam_game_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    video_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    videos: Mapped[list[VideoAsset]] = relationship(
        "VideoAsset", back_populates="folder", cascade="all, delete-orphan"
    )


class VideoAsset(TimestampMixin, Base):
    __tablename__ = "video_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    folder_id: Mapped[str] = mapped_column(ForeignKey("series_folders.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    source_path: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    series_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=VideoStatus.INGESTED.value, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="pt-BR", nullable=False)
    uploaded_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    session_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    folder: Mapped[SeriesFolder] = relationship("SeriesFolder", back_populates="videos")
    drafts: Mapped[list[MetadataDraft]] = relationship(
        "MetadataDraft", back_populates="video", cascade="all, delete-orphan"
    )


class MetadataDraft(TimestampMixin, Base):
    __tablename__ = "metadata_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    video_id: Mapped[str] = mapped_column(ForeignKey("video_assets.id"), index=True, nullable=False)
    title_ptbr: Mapped[str] = mapped_column(String(200), nullable=False)
    description_ptbr: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    model_provider: Mapped[str] = mapped_column(String(64), default="opencloud", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="pt-BR", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    video: Mapped[VideoAsset] = relationship("VideoAsset", back_populates="drafts")


class ChannelDefaults(Base):
    __tablename__ = "channel_defaults"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    channel_name: Mapped[str] = mapped_column(String(255), default="Meu Canal de Gameplay", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="pt-BR", nullable=False)
    default_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    pc_config: Mapped[str] = mapped_column(String(255), default="RTX", nullable=False)
    default_description_block: Mapped[str] = mapped_column(
        Text,
        default="Gameplay sem comentarios. Gravado em PC com RTX.",
        nullable=False,
    )
    default_visibility: Mapped[str] = mapped_column(String(32), default="private", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
