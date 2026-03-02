from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ChannelDefaults


def get_or_create_channel_defaults(db: Session, settings: Settings) -> ChannelDefaults:
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


def normalize_tag_csv(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def update_channel_defaults_from_form(
    defaults: ChannelDefaults,
    *,
    channel_name: str,
    language: str,
    default_tags: str,
    pc_config: str,
    default_description_block: str,
    default_visibility: str,
) -> ChannelDefaults:
    defaults.channel_name = channel_name.strip()
    defaults.language = language.strip() or "pt-BR"
    defaults.default_tags = normalize_tag_csv(default_tags)
    defaults.pc_config = pc_config.strip()
    defaults.default_description_block = default_description_block.strip()
    defaults.default_visibility = default_visibility
    defaults.updated_at = datetime.now(timezone.utc)
    return defaults


def apply_channel_defaults_patch(defaults: ChannelDefaults, updates: dict) -> ChannelDefaults:
    for key, value in updates.items():
        setattr(defaults, key, value)
    defaults.updated_at = datetime.now(timezone.utc)
    return defaults
