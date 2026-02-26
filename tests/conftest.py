from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    video_root = tmp_path / "inbox"
    artifacts_root = tmp_path / "artifacts"
    video_root.mkdir(parents=True, exist_ok=True)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "test.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        video_root=str(video_root),
        artifacts_root=str(artifacts_root),
        api_token=None,
        telegram_webhook_secret=None,
    )

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client, video_root
