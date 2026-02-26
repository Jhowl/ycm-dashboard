from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import VideoAsset
from app.services.metadata import approve_video, generate_metadata_draft, reject_video, upload_video


def handle_telegram_command(db: Session, text: str, settings) -> str:
    command = (text or "").strip()
    if not command:
        return "Comando vazio"

    if command == "/pending":
        pending = db.execute(
            select(VideoAsset).where(VideoAsset.status.in_(["INGESTED", "DRAFT_READY"]))
        ).scalars().all()
        if not pending:
            return "Nenhum video pendente"
        return "Pendentes: " + ", ".join(video.id[:8] for video in pending)

    parts = command.split()
    action = parts[0].lower()
    video_id = parts[1] if len(parts) > 1 else None

    if action in {"/approve", "/upload", "/reject", "/regen", "/video"} and not video_id:
        return "Informe o ID do video. Exemplo: /approve <video_id>"

    if action == "/approve":
        approve_video(db, video_id)
        return f"Video {video_id} aprovado"

    if action == "/upload":
        upload_video(db, video_id)
        return f"Video {video_id} enviado para o YouTube"

    if action == "/reject":
        reject_video(db, video_id)
        return f"Video {video_id} retornou para INGESTED"

    if action == "/regen":
        draft = generate_metadata_draft(db, settings, video_id)
        return f"Draft {draft.id} regenerado para video {video_id}"

    if action == "/video":
        video = db.get(VideoAsset, video_id)
        if not video:
            return "Video nao encontrado"
        return f"Video {video.id}: status={video.status}, arquivo={video.filename}"

    return "Comando nao suportado"
