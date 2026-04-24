from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="YCM_",
        extra="ignore",
    )

    # --- App ---------------------------------------------------------------
    app_name: str = "YouTube Upload Manager"
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Storage / DB ------------------------------------------------------
    database_url: str = "sqlite:///./data/ycm.db"
    redis_url: str = "redis://localhost:6379/0"
    video_root: str = "/srv/ycm/inbox"
    artifacts_root: str = "/srv/ycm/artifacts"
    cuts_root: str = "/srv/ycm/artifacts/cuts"
    transcripts_root: str = "/srv/ycm/artifacts/transcripts"
    scheduler_scan_interval_seconds: int = 300

    default_language: str = "pt-BR"
    default_model_provider: str = "ollama"

    # --- Ollama ------------------------------------------------------------
    ollama_enabled: bool = True
    ollama_base_url: str = "http://192.168.1.182:11434"
    ollama_model: str = "gemma4:26b"
    ollama_timeout_seconds: int = 120

    # --- Whisper / STT -----------------------------------------------------
    whisper_enabled: bool = True
    whisper_model: str = "small"          # tiny, base, small, medium, large-v3
    whisper_device: str = "auto"          # auto, cpu, cuda
    whisper_compute_type: str = "auto"    # auto, int8, float16, float32
    whisper_language: str = "pt"
    whisper_auto_run: bool = True

    # --- Steam -------------------------------------------------------------
    steam_cache_ttl_seconds: int = 180
    steam_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YCM_STEAM_API_KEY", "STEAM_API_KEY"),
    )
    steam_domain: str = Field(
        default="localhost",
        validation_alias=AliasChoices("YCM_STEAM_DOMAIN", "STEAM_DOMAIN"),
    )
    steam_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YCM_STEAM_ID", "STEAM_ID"),
    )

    # --- Integrations ------------------------------------------------------
    telegram_webhook_secret: str | None = None
    api_token: str | None = None
    n8n_webhook_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YCM_N8N_WEBHOOK_URL", "N8N_WEBHOOK_URL"),
    )

    # --- YouTube -----------------------------------------------------------
    youtube_channel_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YCM_YOUTUBE_CHANNEL_ID", "YOUTUBE_CHANNEL_ID"),
    )
    youtube_client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YCM_YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_ID"),
    )
    youtube_client_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YCM_YOUTUBE_CLIENT_SECRET", "YOUTUBE_CLIENT_SECRET"),
    )
    youtube_redirect_uri: str = Field(
        default="http://localhost:8000/ui/youtube/oauth/callback",
        validation_alias=AliasChoices("YCM_YOUTUBE_REDIRECT_URI", "YOUTUBE_REDIRECT_URI"),
    )
    youtube_token_file: str = Field(
        default="./data/youtube_token.json",
        validation_alias=AliasChoices("YCM_YOUTUBE_TOKEN_FILE", "YOUTUBE_TOKEN_FILE"),
    )
    dry_run: bool = Field(default=True, validation_alias=AliasChoices("YCM_DRY_RUN", "DRY_RUN"))

    # --- Legacy ------------------------------------------------------------
    opencloud_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENCLOUD_API_KEY", "YCM_OPENCLOUD_API_KEY"),
    )


def _coerce_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()

    # Backward-compatible fallbacks
    settings.youtube_client_id = (
        settings.youtube_client_id
        or os.getenv("YCM_YOUTUBE_CLIENT_ID")
        or os.getenv("YOUTUBE_CLIENT_ID")
    )
    settings.youtube_client_secret = (
        settings.youtube_client_secret
        or os.getenv("YCM_YOUTUBE_CLIENT_SECRET")
        or os.getenv("YOUTUBE_CLIENT_SECRET")
    )
    settings.youtube_channel_id = (
        settings.youtube_channel_id
        or os.getenv("YCM_YOUTUBE_CHANNEL_ID")
        or os.getenv("YOUTUBE_CHANNEL_ID")
    )
    settings.youtube_redirect_uri = (
        settings.youtube_redirect_uri
        or os.getenv("YCM_YOUTUBE_REDIRECT_URI")
        or os.getenv("YOUTUBE_REDIRECT_URI")
        or "http://localhost:8000/ui/youtube/oauth/callback"
    )
    settings.youtube_token_file = (
        settings.youtube_token_file
        or os.getenv("YCM_YOUTUBE_TOKEN_FILE")
        or os.getenv("YOUTUBE_TOKEN_FILE")
        or "./data/youtube_token.json"
    )
    settings.cuts_root = (
        settings.cuts_root
        or os.getenv("YCM_CUTS_ROOT")
        or os.getenv("CUTS_ROOT")
        or "/srv/ycm/artifacts/cuts"
    )
    settings.transcripts_root = (
        settings.transcripts_root
        or os.getenv("YCM_TRANSCRIPTS_ROOT")
        or "/srv/ycm/artifacts/transcripts"
    )
    settings.steam_api_key = (
        settings.steam_api_key
        or os.getenv("YCM_STEAM_API_KEY")
        or os.getenv("STEAM_API_KEY")
    )
    settings.steam_id = settings.steam_id or os.getenv("YCM_STEAM_ID") or os.getenv("STEAM_ID")
    settings.steam_domain = (
        settings.steam_domain
        or os.getenv("YCM_STEAM_DOMAIN")
        or os.getenv("STEAM_DOMAIN")
        or "localhost"
    )
    settings.n8n_webhook_url = (
        settings.n8n_webhook_url
        or os.getenv("YCM_N8N_WEBHOOK_URL")
        or os.getenv("N8N_WEBHOOK_URL")
    )

    dry_run_env = os.getenv("YCM_DRY_RUN") or os.getenv("DRY_RUN")
    if dry_run_env is not None:
        settings.dry_run = _coerce_bool(dry_run_env, settings.dry_run)

    # Ollama / Whisper toggles (env-first)
    settings.ollama_enabled = _coerce_bool(os.getenv("YCM_OLLAMA_ENABLED"), settings.ollama_enabled)
    settings.whisper_enabled = _coerce_bool(os.getenv("YCM_WHISPER_ENABLED"), settings.whisper_enabled)
    settings.whisper_auto_run = _coerce_bool(os.getenv("YCM_WHISPER_AUTO_RUN"), settings.whisper_auto_run)
    settings.log_json = _coerce_bool(os.getenv("YCM_LOG_JSON"), settings.log_json)

    for path_str in (settings.video_root, settings.artifacts_root, settings.cuts_root, settings.transcripts_root):
        try:
            Path(path_str).mkdir(parents=True, exist_ok=True)
        except OSError:
            # Non-fatal in tests/dev when /srv is not writable
            pass

    return settings
