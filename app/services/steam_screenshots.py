from __future__ import annotations

import re
from typing import Any

import httpx


def fetch_steam_screenshots(steam_id: str, app_id: int | None, limit: int = 20) -> list[dict[str, Any]]:
    if not steam_id:
        return []

    app_q = f"?appid={app_id}&sort=newestfirst&browsefilter=myfiles&view=imagewall" if app_id else "?sort=newestfirst&browsefilter=myfiles&view=imagewall"
    url = f"https://steamcommunity.com/profiles/{steam_id}/screenshots/{app_q}"

    try:
        html = httpx.get(url, timeout=12.0, headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception:
        return []

    # Parse pairs: published file id + background-image url
    pattern = re.compile(
        r"filedetails/\?id=(\d+)[\s\S]{0,500}?background-image:\s*url\('([^']+)'\)",
        re.IGNORECASE,
    )

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in pattern.finditer(html):
        fid, img = m.group(1), m.group(2)
        if fid in seen:
            continue
        seen.add(fid)
        out.append(
            {
                "id": fid,
                "thumb_url": img,
                "detail_url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={fid}",
            }
        )
        if len(out) >= limit:
            break

    return out
