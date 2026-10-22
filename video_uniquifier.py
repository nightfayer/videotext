import asyncio
import os
import subprocess
import sys
from pathlib import Path
from audio_utils import get_ffmpeg_executable


def has_audio_stream(input_file: Path) -> bool:
    """Checks whether the media file contains an audio stream."""
    ffmpeg_bin = get_ffmpeg_executable()
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-i", str(input_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10
        )
        output = res.stderr.decode("utf-8", errors="ignore")
        return "Audio:" in output
    except Exception:
        return False


def uniquify_video_ffmpeg(input_file: Path, output_file: Path) -> tuple[bool, str]:
    """
    Uniquifies a video file for Instagram Reels / TikTok / Shorts without perceptible quality loss.
    Applies:
    - Micro zoom (1.5%) + crop to alter spatial pHash
    - Subtle perceptual color grading (contrast/saturation/brightness)
    - Imperceptible temporal digital grain to defeat frame hashes
    - Micro speed shift (0.8%) to change video timing and audio pitch
    - Full metadata wipe + realistic QuickTime container tags
    - Near-lossless CRF 18 H.264 encoding with faststart
    """
    if not input_file.exists():
        return False, "Входной файл не найден на сервере."
    if input_file.stat().st_size == 0:
        return False, "Входной файл пуст (0 байт)."

    ffmpeg_bin = get_ffmpeg_executable()

    # Video filters: micro zoom + crop, micro color grade, fine noise, micro speed
    vf_filters = [
        "scale=ceil(iw*1.015/2)*2:ceil(ih*1.015/2)*2",
        "crop=iw:ih",
        "eq=contrast=1.015:brightness=0.005:saturation=1.02",
        "noise=alls=2:allf=t",
        "setpts=PTS/1.008"
    ]
    vf_str = ",".join(vf_filters)

    with_audio = has_audio_stream(input_file)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-threads", "4",
        "-i", str(input_file),
        "-vf", vf_str,
        "-c:v", "libx264",
        "-crf", "18",            # Visually lossless quality (default is 23, 18 is near-lossless)
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-map_metadata", "-1",   # Strip all previous metadata
        "-metadata", "title=",
        "-metadata", "comment=Apple QuickTime",
    ]

    if with_audio:
        cmd.extend([
            "-af", "atempo=1.008,volume=1.01",
            "-c:a", "aac",
            "-b:a", "192k"
        ])
    else:
        cmd.append("-an")

    cmd.append(str(output_file))

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1800  # 30 min max for huge files
        )
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="ignore").strip()
            print(f"[Uniquifier FFmpeg Error]: {err}", flush=True)
            return False, f"Ошибка FFmpeg при уникализации: {err[:150]}"

        if not output_file.exists() or output_file.stat().st_size == 0:
            return False, "Уникализированный файл не был создан или пуст."

        return True, ""
    except Exception as e:
        return False, f"Ошибка при уникализации видео: {str(e)}"


async def async_uniquify_video(input_file: Path, output_file: Path) -> tuple[bool, str]:
    """Runs video uniquification asynchronously in thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, uniquify_video_ffmpeg, input_file, output_file)
