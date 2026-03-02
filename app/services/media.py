from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class VideoProbe:
    @staticmethod
    def duration_seconds(video_path: Path) -> int | None:
        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]

        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None

        value = (result.stdout or "").strip()
        if not value:
            return None

        try:
            return int(float(value))
        except ValueError:
            return None


class EpisodeThumbnailRenderer:
    @staticmethod
    def render(
        *,
        video_path: Path,
        output_path: Path,
        episode_number: int,
        thumbnail_prompt: str | None,
    ) -> str | None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        base_path = output_path.with_name(f"{output_path.stem}_base{output_path.suffix}")

        extract_command = [
            "ffmpeg",
            "-y",
            "-ss",
            "00:00:05",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(base_path),
        ]

        try:
            subprocess.run(extract_command, capture_output=True, check=True, timeout=30)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None

        headline = f"EP {episode_number:02d}"
        prompt_text = (thumbnail_prompt or "").strip()[:70]
        draw_filters = [
            "drawbox=x=0:y=ih-160:w=iw:h=160:color=black@0.58:t=fill",
            (
                "drawtext=text='{}':x=30:y=ih-132:fontsize=56:fontcolor=white:"
                "box=0:shadowcolor=black@0.8:shadowx=2:shadowy=2"
            ).format(_escape_drawtext_value(headline)),
        ]
        if prompt_text:
            draw_filters.append(
                (
                    "drawtext=text='{}':x=30:y=ih-66:fontsize=34:fontcolor=white:"
                    "box=0:shadowcolor=black@0.8:shadowx=2:shadowy=2"
                ).format(_escape_drawtext_value(prompt_text))
            )

        overlay_command = [
            "ffmpeg",
            "-y",
            "-i",
            str(base_path),
            "-vf",
            ",".join(draw_filters),
            str(output_path),
        ]

        try:
            subprocess.run(overlay_command, capture_output=True, check=True, timeout=30)
            return str(output_path)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            try:
                shutil.copyfile(base_path, output_path)
                return str(output_path)
            except OSError:
                return str(base_path)
        finally:
            if base_path.exists():
                base_path.unlink(missing_ok=True)


def _escape_drawtext_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    escaped = escaped.replace("%", "\\%")
    escaped = escaped.replace("\n", " ")
    return escaped
