"""Runtime settings overrides backed by the channel_defaults singleton row.

Lets users change AI model selection from the Settings page without a redeploy.
Falls back to env-loaded ``Settings`` whenever the override column is NULL.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ChannelDefaults


_OVERRIDE_FIELDS = (
    ("ai_ollama_model", "ollama_model"),
    ("ai_ollama_enabled", "ollama_enabled"),
    ("ai_whisper_model", "whisper_model"),
    ("ai_whisper_enabled", "whisper_enabled"),
    ("ai_whisper_auto_run", "whisper_auto_run"),
)


@dataclass(frozen=True)
class RuntimeAISettings:
    ollama_model: str
    ollama_enabled: bool
    whisper_model: str
    whisper_enabled: bool
    whisper_auto_run: bool

    # Which of the above are *currently overriding* env defaults.
    overridden: frozenset[str]


def _get_or_init_defaults(db: Session) -> ChannelDefaults:
    row = db.get(ChannelDefaults, 1)
    if row is None:
        row = ChannelDefaults(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_runtime_ai(db: Session, settings: Settings) -> RuntimeAISettings:
    """Read effective AI settings (env + DB override)."""
    row = _get_or_init_defaults(db)
    overridden: set[str] = set()
    values: dict[str, object] = {}
    for col, attr in _OVERRIDE_FIELDS:
        raw = getattr(row, col, None)
        if raw is None:
            values[attr] = getattr(settings, attr)
        else:
            overridden.add(attr)
            if attr in {"ollama_enabled", "whisper_enabled", "whisper_auto_run"}:
                values[attr] = bool(raw)
            else:
                values[attr] = raw
    return RuntimeAISettings(overridden=frozenset(overridden), **values)  # type: ignore[arg-type]


def apply_runtime_overrides(settings: Settings, db: Session) -> Settings:
    """Return a copy of ``settings`` with DB overrides applied (in-place safe).

    Used by ``worker_session`` and request-scope code paths that consume Ollama
    or Whisper, so the user-selected model wins without a process restart.
    """
    runtime = get_runtime_ai(db, settings)
    return settings.model_copy(
        update={
            "ollama_model": runtime.ollama_model,
            "ollama_enabled": runtime.ollama_enabled,
            "whisper_model": runtime.whisper_model,
            "whisper_enabled": runtime.whisper_enabled,
            "whisper_auto_run": runtime.whisper_auto_run,
        }
    )


def update_runtime_ai(
    db: Session,
    *,
    ollama_model: str | None | object = ...,
    ollama_enabled: bool | None | object = ...,
    whisper_model: str | None | object = ...,
    whisper_enabled: bool | None | object = ...,
    whisper_auto_run: bool | None | object = ...,
) -> ChannelDefaults:
    """Patch override values. Pass ``None`` to clear an override."""
    row = _get_or_init_defaults(db)
    locals_ = locals()
    mapping = {
        "ai_ollama_model": "ollama_model",
        "ai_ollama_enabled": "ollama_enabled",
        "ai_whisper_model": "whisper_model",
        "ai_whisper_enabled": "whisper_enabled",
        "ai_whisper_auto_run": "whisper_auto_run",
    }
    for col, kwarg in mapping.items():
        val = locals_[kwarg]
        if val is ...:
            continue
        setattr(row, col, val)
    db.commit()
    db.refresh(row)
    return row
