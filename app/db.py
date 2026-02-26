from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_engine_and_session_factory(database_url: str):
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory


def init_db(engine) -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_series_folder_columns(engine)
    _ensure_video_asset_columns(engine)


def _ensure_series_folder_columns(engine) -> None:
    inspector = inspect(engine)
    if "series_folders" not in inspector.get_table_names():
        return

    current_columns = {column["name"] for column in inspector.get_columns("series_folders")}
    missing_columns: list[tuple[str, str]] = []
    if "steam_app_id" not in current_columns:
        missing_columns.append(("steam_app_id", "INTEGER"))
    if "steam_game_name" not in current_columns:
        missing_columns.append(("steam_game_name", "TEXT"))

    if not missing_columns:
        return

    with engine.begin() as connection:
        for column_name, column_type in missing_columns:
            if engine.dialect.name == "postgresql":
                statement = (
                    f"ALTER TABLE series_folders ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                )
            else:
                statement = f"ALTER TABLE series_folders ADD COLUMN {column_name} {column_type}"
            connection.execute(text(statement))


def _ensure_video_asset_columns(engine) -> None:
    inspector = inspect(engine)
    if "video_assets" not in inspector.get_table_names():
        return

    current_columns = {column["name"] for column in inspector.get_columns("video_assets")}
    missing_columns: list[tuple[str, str]] = []
    if "series_number" not in current_columns:
        missing_columns.append(("series_number", "INTEGER"))
    if "thumbnail_prompt" not in current_columns:
        missing_columns.append(("thumbnail_prompt", "TEXT"))

    if not missing_columns:
        return

    with engine.begin() as connection:
        for column_name, column_type in missing_columns:
            if engine.dialect.name == "postgresql":
                statement = (
                    f"ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                )
            else:
                statement = f"ALTER TABLE video_assets ADD COLUMN {column_name} {column_type}"
            connection.execute(text(statement))


def get_db(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    db: Session = session_factory()
    try:
        yield db
    finally:
        db.close()
