from __future__ import annotations

from pathlib import Path
import subprocess

from app.models import VideoAsset


def _gpu_ffmpeg_args() -> list[str]:
    """Best-effort CUDA decode args; silently fallback to CPU if unavailable."""
    try:
        probe = subprocess.run(["nvidia-smi"], capture_output=True, timeout=3)
        if probe.returncode == 0:
            return ["-hwaccel", "cuda"]
    except Exception:
        pass
    return []


def thumbnail_lab_dir(video_id: str) -> Path:
    return Path("data/thumbnail_lab") / video_id


def ensure_thumbnail_lab_assets(video: VideoAsset, force_regen: bool = False) -> list[str]:
    out_dir = thumbnail_lab_dir(video.id)
    out_dir.mkdir(parents=True, exist_ok=True)

    expected = [f"option_{i}.jpg" for i in range(1, 21)]
    if force_regen:
        for name in expected:
            p = out_dir / name
            if p.exists():
                p.unlink(missing_ok=True)

    if all((out_dir / name).exists() for name in expected):
        return expected

    source = Path(video.source_path)
    if not source.exists():
        return []

    try:
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)
        ], text=True).strip()
        duration = max(1, int(float(probe)))
    except Exception:
        duration = 1

    payload = video.session_payload or {}
    anchors: list[int] = []
    detailed = payload.get("achievements_unlocked_detailed") or []
    if video.recorded_at and isinstance(detailed, list):
        start_ts = int(video.recorded_at.timestamp())
        for item in detailed:
            try:
                unlock_ts = int(item.get("unlocktime", 0))
            except Exception:
                unlock_ts = 0
            if unlock_ts > 0:
                off = max(1, min(duration - 1, unlock_ts - start_ts))
                anchors.extend([max(1, off - 8), off, min(duration - 1, off + 8)])

    timeline = [0.03,0.07,0.11,0.15,0.19,0.23,0.27,0.31,0.35,0.39,0.43,0.47,0.51,0.55,0.59,0.63,0.67,0.71,0.75,0.79,0.83,0.87,0.91,0.95]
    anchors.extend([max(1, int(duration * p)) for p in timeline])

    dedup: list[int] = []
    for a in anchors:
        if a not in dedup:
            dedup.append(a)

    gpu_args = _gpu_ffmpeg_args()

    generated: list[str] = []
    for second in dedup:
        if len(generated) >= 20:
            break
        filename = f"option_{len(generated)+1}.jpg"
        output = out_dir / filename

        w_start = max(0, second - 14)
        w_end = min(duration, second + 14)
        try:
            subprocess.run([
                "ffmpeg", "-y", *gpu_args, "-ss", str(w_start), "-to", str(w_end), "-i", str(source),
                "-vf", "select=gt(scene\,0.27)", "-frames:v", "1", "-q:v", "2", str(output)
            ], check=True, capture_output=True, timeout=45)
        except Exception:
            try:
                subprocess.run([
                    "ffmpeg", "-y", "-ss", str(second), "-i", str(source), "-frames:v", "1", "-q:v", "2", str(output)
                ], check=True, capture_output=True, timeout=30)
            except Exception:
                continue

        if output.exists():
            generated.append(filename)

    return generated
