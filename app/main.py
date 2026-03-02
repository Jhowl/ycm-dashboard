from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.db import create_engine_and_session_factory, init_db
from app.routers.api import router as api_router
from app.routers.ui import router as ui_router


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    init_db(app.state.engine)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    engine, session_factory = create_engine_and_session_factory(app_settings.database_url)

    app = FastAPI(title=app_settings.app_name, lifespan=app_lifespan)
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    Path(app_settings.video_root).mkdir(parents=True, exist_ok=True)
    Path(app_settings.artifacts_root).mkdir(parents=True, exist_ok=True)

    app.include_router(api_router)
    app.include_router(ui_router)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def ready() -> dict:
        return {"ok": True}

    return app


app = create_app()
