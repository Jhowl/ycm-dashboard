"""Database engine, session factory, and lightweight schema sync."""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_engine_and_session_factory(database_url: str):
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory


# Columns that may be missing on older databases.
# Format: {table: [(column_name, column_type, default_literal|None), ...]}
_EXPECTED_COLUMNS: dict[str, list[tuple[str, str, str | None]]] = {
    "series_folders": [
        ("steam_app_id", "INTEGER", None),
        ("steam_game_name", "TEXT", None),
    ],
    "video_assets": [
        ("series_number", "INTEGER", None),
        ("thumbnail_prompt", "TEXT", None),
        ("transcript_status", "VARCHAR(16)", "'PENDING'"),
        ("transcript_path", "TEXT", None),
        ("transcript_language", "VARCHAR(16)", None),
        ("transcript_text", "TEXT", None),
        ("transcript_error", "TEXT", None),
        ("chapters", "JSON", None),
    ],
    "metadata_drafts": [
        ("chapters", "JSON", None),
        ("thumbnail_prompt", "TEXT", None),
        ("model_name", "VARCHAR(128)", None),
    ],
    "channel_defaults": [
        ("ai_ollama_url", "VARCHAR(255)", None),
        ("ai_ollama_model", "VARCHAR(128)", None),
        ("ai_ollama_enabled", "BOOLEAN", None),
        ("ai_whisper_model", "VARCHAR(128)", None),
        ("ai_whisper_enabled", "BOOLEAN", None),
        ("ai_whisper_auto_run", "BOOLEAN", None),
    ],
}


def init_db(engine) -> None:
    from app import models  # noqa: F401 — register models with Base.metadata

    Base.metadata.create_all(bind=engine)
    _ensure_expected_columns(engine)


def _ensure_expected_columns(engine) -> None:
    inspector = inspect(engine)
    dialect = engine.dialect.name

    for table, columns in _EXPECTED_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        current = {col["name"] for col in inspector.get_columns(table)}
        missing = [spec for spec in columns if spec[0] not in current]
        if not missing:
            continue

        with engine.begin() as connection:
            for name, ctype, default in missing:
                coltype = _translate_type(ctype, dialect)
                default_clause = f" DEFAULT {default}" if default else ""
                if_not_exists = "IF NOT EXISTS " if dialect == "postgresql" else ""
                statement = (
                    f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{name} {coltype}{default_clause}"
                )
                connection.execute(text(statement))


def _translate_type(ctype: str, dialect: str) -> str:
    if ctype.upper() == "JSON":
        # SQLite doesn't support JSON natively pre-3.9; TEXT is fine for our usage.
        if dialect == "sqlite":
            return "TEXT"
    return ctype


def get_db(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    db: Session = session_factory()
    try:
        yield db
    finally:
        db.close()
