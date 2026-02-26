from __future__ import annotations

import time

from app.config import get_settings
from app.db import create_engine_and_session_factory, init_db
from app.services.folders import sync_folders_and_videos


def main() -> None:
    settings = get_settings()
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    init_db(engine)

    interval = max(60, settings.scheduler_scan_interval_seconds)
    print(f"[scheduler] starting with interval={interval}s")

    while True:
        with session_factory() as db:
            result = sync_folders_and_videos(db, settings)
            print(f"[scheduler] scan={result}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
