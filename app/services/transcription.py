"""Whisper-based speech-to-text pipeline.

The expensive parts (model load + transcribe) are lazy: :mod:`faster_whisper`
is only imported when :func:`transcribe_video` is invoked. This keeps unit tests
fast and lets the project import cleanly in environments without GPU deps.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import TranscriptStatus, VideoAsset

logger = get_logger(__name__)

# Keep a single Whisper model loaded per worker process.
_MODEL_CACHE: dict[str, Any] = {}


@dataclass(slots=True)
class TranscriptResult:
    language: str
    segments: list[dict[str, Any]]
    text: str
    transcript_path: str
    srt_path: str | None


def transcribe_video(db: Session, settings: Settings, video_id: str) -> TranscriptResult:
    """Run Whisper on the video's audio track and persist outputs."""
    video = db.get(VideoAsset, video_id)
    if video is None:
        raise ValueError("Video not found")

    if not settings.whisper_enabled:
        video.transcript_status = TranscriptStatus.SKIPPED.value
        video.transcript_error = "whisper disabled"
        db.commit()
        raise RuntimeError("Whisper is disabled via YCM_WHISPER_ENABLED=false")

    source = Path(video.source_path)
    if not source.exists():
        video.transcript_status = TranscriptStatus.FAILED.value
        video.transcript_error = "source video missing"
        db.commit()
        raise FileNotFoundError(f"Source video missing: {source}")

    _mark_running(db, video)

    transcripts_root = Path(settings.transcripts_root) / video_id
    transcripts_root.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = _extract_audio(source, Path(tmpdir) / "audio.wav")
            result = _run_whisper(settings, audio_path)

        segments = result["segments"]
        text = result["text"]
        language = result["language"]

        transcript_json = transcripts_root / "transcript.json"
        transcript_json.write_text(
            json.dumps(
                {"language": language, "segments": segments, "text": text},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        srt_path = transcripts_root / "transcript.srt"
        srt_path.write_text(_segments_to_srt(segments), encoding="utf-8")

        _mark_ready(db, video, transcript_json, language, text)

        return TranscriptResult(
            language=language,
            segments=segments,
            text=text,
            transcript_path=str(transcript_json),
            srt_path=str(srt_path),
        )
    except Exception as exc:  # noqa: BLE001 — we log and re-raise
        _mark_failed(db, video, exc)
        logger.exception("transcription_failed", extra={"video_id": video_id})
        raise


# ------ helpers ---------------------------------------------------------
def _extract_audio(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=60 * 30)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is not available") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffmpeg failed extracting audio: {exc.stderr.decode('utf-8', 'ignore')[:400]}"
        ) from exc
    return destination


def _get_model(settings: Settings):
    key = f"{settings.whisper_model}|{settings.whisper_device}|{settings.whisper_compute_type}"
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Add it to requirements.txt"
        ) from exc

    device = None if settings.whisper_device == "auto" else settings.whisper_device
    compute_type = None if settings.whisper_compute_type == "auto" else settings.whisper_compute_type

    kwargs: dict[str, Any] = {"model_size_or_path": settings.whisper_model}
    if device:
        kwargs["device"] = device
    if compute_type:
        kwargs["compute_type"] = compute_type

    logger.info("whisper_loading_model", extra={"model": settings.whisper_model, "device": device or "auto"})
    model = WhisperModel(**kwargs)
    _MODEL_CACHE[key] = model
    return model


def _run_whisper(settings: Settings, audio_path: Path) -> dict[str, Any]:
    model = _get_model(settings)
    language = settings.whisper_language or None
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
    )

    segments: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for seg in segments_iter:
        clean = (seg.text or "").strip()
        if not clean:
            continue
        segments.append(
            {
                "start": round(float(seg.start or 0), 2),
                "end": round(float(seg.end or 0), 2),
                "text": clean,
            }
        )
        text_parts.append(clean)

    return {
        "language": getattr(info, "language", language or "unknown") or "unknown",
        "segments": segments,
        "text": " ".join(text_parts).strip(),
    }


def _segments_to_srt(segments: list[dict[str, Any]]) -> str:
    def fmt(ts: float) -> str:
        total_ms = int(round(float(ts) * 1000))
        hh = total_ms // 3_600_000
        mm = (total_ms // 60_000) % 60
        ss = (total_ms // 1_000) % 60
        ms = total_ms % 1_000
        return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"

    lines: list[str] = []
    for idx, seg in enumerate(segments, start=1):
        lines.append(str(idx))
        lines.append(f"{fmt(seg['start'])} --> {fmt(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def _mark_running(db: Session, video: VideoAsset) -> None:
    video.transcript_status = TranscriptStatus.RUNNING.value
    video.transcript_error = None
    db.commit()


def _mark_ready(
    db: Session, video: VideoAsset, transcript_json: Path, language: str, text: str
) -> None:
    video.transcript_status = TranscriptStatus.READY.value
    video.transcript_path = str(transcript_json)
    video.transcript_language = language
    video.transcript_text = text[:20000]
    video.transcript_error = None
    db.commit()


def _mark_failed(db: Session, video: VideoAsset, exc: BaseException) -> None:
    video.transcript_status = TranscriptStatus.FAILED.value
    video.transcript_error = str(exc)[:500]
    try:
        db.commit()
    except Exception:
        db.rollback()


def load_transcript(video: VideoAsset) -> dict[str, Any] | None:
    """Return parsed transcript JSON from disk, if available."""
    if not video.transcript_path:
        return None

    path = Path(video.transcript_path)
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def transcript_exists_for(video: VideoAsset) -> bool:
    if not video.transcript_path:
        return False
    return Path(video.transcript_path).exists()


def delete_transcript(video: VideoAsset) -> None:
    if not video.transcript_path:
        return
    parent = Path(video.transcript_path).parent
    if parent.exists():
        shutil.rmtree(parent, ignore_errors=True)
