from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Settings, get_settings
from app.db import create_engine_and_session_factory, init_db


@contextmanager
def worker_session(settings: Settings | None = None) -> Iterator[tuple[Settings, object]]:
    app_settings = settings or get_settings()
    engine, session_factory = create_engine_and_session_factory(app_settings.database_url)
    init_db(engine)

    with session_factory() as db:
        yield app_settings, db
