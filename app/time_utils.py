from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def format_datetime_ny(value: datetime | None) -> str:
    if value is None:
        return "-"

    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    localized = dt.astimezone(NY_TZ)
    return localized.strftime("%m/%d/%Y %I:%M %p ET")
