from __future__ import annotations

import json
from pathlib import Path

DEFAULTS_PATH = Path("config/game_tag_defaults.json")


def _ensure_file() -> None:
    DEFAULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DEFAULTS_PATH.exists():
        DEFAULTS_PATH.write_text("{}\n", encoding="utf-8")


def get_game_tag_defaults() -> dict[str, list[str]]:
    _ensure_file()
    try:
        raw = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, list[str]] = {}
    for game, tags in raw.items():
        if not isinstance(game, str):
            continue
        if not isinstance(tags, list):
            continue
        values = [str(t).strip() for t in tags if str(t).strip()]
        if values:
            cleaned[game.strip()] = values
    return cleaned


def save_game_tag_defaults(payload: dict[str, list[str]]) -> None:
    _ensure_file()
    DEFAULTS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def game_tag_defaults_text() -> str:
    data = get_game_tag_defaults()
    return json.dumps(data, indent=2, ensure_ascii=False)
