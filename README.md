# YCM Dashboard

Linux-hosted, folder-first YouTube workflow for gameplay channels.

The project scans game folders, ingests videos, generates PT-BR metadata drafts, and manages upload + monitoring via dashboard + API + Telegram commands.

## What it does

- Auto-scan a root video folder and detect game folders as series.
- Create slug URLs per folder: `/series/<folder-slug>`.
- Show dashboard pages for Home, Folders, Series, and Config.
- Generate draft title/description/tags in Portuguese (`pt-BR`).
- Generate thumbnail images with `ffmpeg` and optional per-video prompt.
- Link each folder to a Steam game from your profile.
- Upload to YouTube (real upload with OAuth token + refresh token).
- Support YouTube OAuth token generation from the UI.
- Expose API endpoints for MCP/agent integrations.
- Video Settings page with 20 scene-based thumbnail options.
- Achievement cuts page (`/cuts`) with playback/download.

## Current behavior

- Upload can run in real mode (YouTube Data API) or dry-run mode via env (`YCM_DRY_RUN`).
- Metadata generation is template-based and can be edited per video.
- Upload and thumbnail regeneration run in background tasks (Celery).
- UI supports desktop/tablet responsive layout.

## Stack

- Python 3.12, FastAPI, SQLAlchemy, Jinja2
- Postgres, Redis, Celery worker, scheduler loop
- Docker Compose
- ffmpeg/ffprobe for media duration and thumbnail frame extraction

## Quick start

1. Copy environment template:

```bash
cp .env.example .env
```

2. Start services:

```bash
docker compose up --build -d
```

3. Open dashboard:

```text
http://localhost:8010/
```

4. Run first scan:
- Open `/folders`.
- Click `Scan folders`.

## Folder-first workflow

1. Put videos under your root inbox (`YCM_VIDEO_ROOT`):

```text
/srv/ycm/inbox/Resident Evil 9/2026-02-26_21-10-00.mp4
```

2. Scan folders (`/folders` UI or `POST /api/v1/folders/scan`).
3. Open series page (`/series/<slug>`).
4. Optionally set per-video `series_number` and `thumbnail_prompt`.
5. Generate draft.
6. Upload (auto-generates draft if missing).
7. Track task status in series page.

## UI routes

- `/` Home dashboard (stats, token status, Steam summary, folder list)
- `/folders` Folder discovery and Steam link mapping
- `/series/<slug>` Video cards, actions, and draft workflow
- `/ui/video-settings/<video_id>` Single-page video settings + thumbnail gallery
- `/cuts` Achievement cuts list with open/download
- `/config` Channel defaults and YouTube OAuth setup
- `/ui/youtube/oauth/start` Start OAuth flow
- `/ui/youtube/oauth/callback` OAuth callback route

## YouTube OAuth setup

1. Set these env vars in `.env`:
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
2. Ensure redirect URI in Google Console matches exactly:
- `http://localhost:8010/ui/youtube/oauth/callback`
3. Open `/config` and click `Gerar token YouTube`.
4. Token file is saved at `YCM_YOUTUBE_TOKEN_FILE` (default `./data/youtube_token.json`).

## API summary

All API routes are under `/api/v1`.
If `YCM_API_TOKEN` is set, send header `X-API-Token`.

- `POST /folders/scan`
- `GET /folders`
- `GET /folders/{folder_id}`
- `GET /folders/{folder_id}/url`
- `PATCH /folders/{folder_id}/steam-link`
- `GET /channel/defaults`
- `PATCH /channel/defaults`
- `GET /series/{slug}`
- `GET /videos/{video_id}`
- `POST /videos/{video_id}/generate`
- `PATCH /videos/{video_id}/settings`
- `POST /videos/{video_id}/approve`
- `POST /videos/{video_id}/reject`
- `POST /videos/{video_id}/upload`
- `POST /telegram/webhook`
- `GET /home/stats`

## Telegram commands

- `/pending`
- `/video <video_id>`
- `/approve <video_id>`
- `/reject <video_id>`
- `/upload <video_id>`
- `/regen <video_id>`

## Environment notes

- Main app variables use `YCM_` prefix.
- Plain compatibility keys also work for some integrations (`YOUTUBE_CLIENT_ID`, `STEAM_API_KEY`, etc.).
- Do not commit `.env`, tokens, or secrets.

## Development

Run tests in container:

```bash
docker compose exec -T api pytest -q
```

Useful commands:

```bash
docker compose logs -f api
docker compose logs -f worker
```
