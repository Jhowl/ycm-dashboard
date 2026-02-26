from __future__ import annotations

from pathlib import Path


def _create_video(path: Path, content: bytes = b"fake video") -> None:
    path.write_bytes(content)


def _scan(client):
    response = client.post("/api/v1/folders/scan", json={})
    assert response.status_code == 200
    return response.json()


def _first_video_id(client, folder_id: str) -> str:
    folder_detail = client.get(f"/api/v1/folders/{folder_id}")
    assert folder_detail.status_code == 200
    videos = folder_detail.json()["videos"]
    assert videos
    return videos[0]["id"]


def test_scan_discovers_game_folder_and_slug_url(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")

    data = _scan(http)
    assert data["discovered_folders"] == 1
    assert data["new_folders"] == 1

    folders = http.get("/api/v1/folders")
    assert folders.status_code == 200
    items = folders.json()
    assert len(items) == 1
    assert items[0]["name"] == "Resident Evil 9"
    assert items[0]["slug"] == "resident-evil-9"
    assert items[0]["series_url"] == "/series/resident-evil-9"


def test_folder_rename_deactivates_old_and_creates_new(client):
    http, video_root = client
    old_folder = video_root / "Resident Evil 9"
    old_folder.mkdir()
    _create_video(old_folder / "2026-02-26_20-00-00.mp4")

    _scan(http)

    new_folder = video_root / "Resident Evil 9 Remake"
    old_folder.rename(new_folder)
    _create_video(new_folder / "2026-02-27_21-00-00.mp4")

    _scan(http)

    folders = http.get("/api/v1/folders", params={"include_inactive": "true"})
    assert folders.status_code == 200
    data = folders.json()

    assert len(data) == 2
    inactive = [item for item in data if not item["active"]]
    active = [item for item in data if item["active"]]
    assert len(inactive) == 1
    assert len(active) == 1
    assert inactive[0]["name"] == "Resident Evil 9"
    assert active[0]["name"] == "Resident Evil 9 Remake"


def test_folder_video_count_is_accurate(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")
    _create_video(folder / "2026-02-26_22-10-00.mkv")

    _scan(http)

    folders = http.get("/api/v1/folders")
    assert folders.status_code == 200
    assert folders.json()[0]["video_count"] == 2


def test_home_defaults_save_load_and_apply_to_generated_draft(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")

    _scan(http)

    patch = http.patch(
        "/api/v1/channel/defaults",
        json={
            "channel_name": "Canal Teste",
            "default_tags": ["resident evil 9", "gameplay"],
            "default_description_block": "Bloco fixo da descricao PT-BR.",
            "pc_config": "RTX 4080",
        },
    )
    assert patch.status_code == 200

    defaults = http.get("/api/v1/channel/defaults")
    assert defaults.status_code == 200
    body = defaults.json()
    assert body["channel_name"] == "Canal Teste"
    assert body["pc_config"] == "RTX 4080"

    folders = http.get("/api/v1/folders").json()
    folder_id = folders[0]["id"]
    video_id = _first_video_id(http, folder_id)

    generated = http.post(f"/api/v1/videos/{video_id}/generate")
    assert generated.status_code == 200

    series = http.get("/api/v1/series/resident-evil-9")
    assert series.status_code == 200
    description = series.json()["videos"][0]["latest_draft"]["description_ptbr"]
    assert "Bloco fixo da descricao PT-BR." in description


def test_ai_output_remains_ptbr_and_uses_defaults_block(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")

    _scan(http)

    http.patch(
        "/api/v1/channel/defaults",
        json={"default_description_block": "Texto default em portugues."},
    )

    folder_id = http.get("/api/v1/folders").json()[0]["id"]
    video_id = _first_video_id(http, folder_id)

    result = http.post(f"/api/v1/videos/{video_id}/generate")
    assert result.status_code == 200

    video = http.get(f"/api/v1/videos/{video_id}")
    assert video.status_code == 200
    payload = video.json()
    assert payload["latest_draft"]["language"] == "pt-BR"
    assert "Gameplay PT-BR" in payload["latest_draft"]["title_ptbr"]
    assert "Texto default em portugues." in payload["latest_draft"]["description_ptbr"]


def test_series_number_and_thumbnail_prompt_flow_into_title_and_video_settings(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")

    _scan(http)

    folder_id = http.get("/api/v1/folders").json()[0]["id"]
    video_id = _first_video_id(http, folder_id)

    update = http.patch(
        f"/api/v1/videos/{video_id}/settings",
        json={
            "series_number": 7,
            "thumbnail_prompt": "Chefao no laboratorio com pouca vida",
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["series_number"] == 7
    assert body["thumbnail_prompt"] == "Chefao no laboratorio com pouca vida"

    generated = http.post(f"/api/v1/videos/{video_id}/generate")
    assert generated.status_code == 200

    video = http.get(f"/api/v1/videos/{video_id}")
    assert video.status_code == 200
    payload = video.json()
    assert payload["latest_draft"]["title_ptbr"].endswith("Episodio 07")
    assert "Prompt thumbnail: Chefao no laboratorio com pouca vida" in payload["latest_draft"][
        "description_ptbr"
    ]


def test_series_url_opens_correct_folder_video_list(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")

    _scan(http)
    folder_item = http.get("/api/v1/folders").json()[0]

    folder_url = http.get(f"/api/v1/folders/{folder_item['id']}/url")
    assert folder_url.status_code == 200

    series_page = http.get(folder_url.json()["series_url"])
    assert series_page.status_code == 200
    assert "2026-02-26_21-10-00.mp4" in series_page.text


def test_upload_requires_approval(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")

    _scan(http)
    folder_id = http.get("/api/v1/folders").json()[0]["id"]
    video_id = _first_video_id(http, folder_id)

    blocked = http.post(f"/api/v1/videos/{video_id}/upload")
    assert blocked.status_code == 409

    approved = http.post(f"/api/v1/videos/{video_id}/approve")
    assert approved.status_code == 200

    uploaded = http.post(f"/api/v1/videos/{video_id}/upload")
    assert uploaded.status_code == 200


def test_telegram_approve_updates_same_state_as_ui_approve(client):
    http, video_root = client
    folder = video_root / "Resident Evil 9"
    folder.mkdir()
    _create_video(folder / "2026-02-26_21-10-00.mp4")
    _create_video(folder / "2026-02-26_22-10-00.mp4")

    _scan(http)

    folder_id = http.get("/api/v1/folders").json()[0]["id"]
    detail = http.get(f"/api/v1/folders/{folder_id}").json()
    ids = [video["id"] for video in detail["videos"]]

    ui_resp = http.post(f"/ui/videos/{ids[0]}/approve", follow_redirects=False)
    assert ui_resp.status_code == 303

    tg_resp = http.post(
        "/api/v1/telegram/webhook",
        json={"message": {"text": f"/approve {ids[1]}"}},
    )
    assert tg_resp.status_code == 200

    v1 = http.get(f"/api/v1/videos/{ids[0]}").json()
    v2 = http.get(f"/api/v1/videos/{ids[1]}").json()
    assert v1["status"] == "APPROVED"
    assert v2["status"] == "APPROVED"
