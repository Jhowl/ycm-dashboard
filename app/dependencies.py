from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from app.config import Settings


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_api_token(
    request: Request,
    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
) -> None:
    settings: Settings = request.app.state.settings
    if not settings.api_token:
        return

    if x_api_token != settings.api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token")
