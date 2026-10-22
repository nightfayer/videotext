import asyncio
import datetime
import os
import random
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
    Ultra-resilient video uniquifier optimized for servers without GPU.
    Uses randomized parameters on EVERY run to guarantee uniqueness:
    - Micro zoom (2.0% - 2.8%) + Micro-rotation (0.3° - 0.5°) to break spatial pHash grids
    - Dynamic subtle color grading (contrast/saturation/brightness)
    - Temporal digital noise to defeat per-frame bitwise matching
    - Micro speed/tempo shift (0.6% - 1.4%) to disrupt audio fingerprints and keyframe timings
    - Complete metadata spoofing under Apple iPhone 15 Pro Max (iOS 17.5.1, QuickTime)
    - High-speed CPU encoding via preset 'veryfast' + CRF 18 (visually lossless quality)
    """
    if not input_file.exists():
        return False, "Входной файл не найден на сервере."
    if input_file.stat().st_size == 0:
        return False, "Входной файл пуст (0 байт)."

    ffmpeg_bin = get_ffmpeg_executable()

    # 1. Randomized parameter generation (every output file gets unique fingerprints)
    zoom = round(random.uniform(1.020, 1.028), 3)
    angle_deg = round(random.choice([-1, 1]) * random.uniform(0.30, 0.50), 2)
    speed = round(random.uniform(1.006, 1.014), 3)
    contrast = round(random.uniform(1.010, 1.022), 3)
    saturation = round(random.uniform(1.010, 1.022), 3)
    brightness = round(random.uniform(0.003, 0.007), 3)
    noise_seed = random.randint(1000, 999999)

    # Single-pass filtergraph: Zoom -> Rotate -> Crop to original size -> Color grade -> Temporal noise -> Speed PTS
    vf_filters = [
        f"scale=ceil(iw*{zoom}/2)*2:ceil(ih*{zoom}/2)*2",
        f"rotate={angle_deg}*PI/180:ow=iw:oh=ih",
        "crop=iw:ih",
        f"eq=contrast={contrast}:brightness={brightness}:saturation={saturation}",
        f"noise=alls=2:allf=t:all_seed={noise_seed}",
        f"setpts=PTS/{speed}"
    ]
    vf_str = ",".join(vf_filters)

    with_audio = has_audio_stream(input_file)

    # 2. Fake iPhone 15 Pro creation timestamp (10-30 minutes ago)
    recent_time = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=random.randint(12, 35))
    ).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-threads", "0",          # Auto-detect optimal CPU threads without bottlenecking
        "-i", str(input_file),
        "-vf", vf_str,
        "-c:v", "libx264",
        "-crf", "18",             # Visually lossless studio quality
        "-preset", "veryfast",    # Ultra-optimized for CPU: 2.5-3x faster than 'medium'
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-map_metadata", "-1",    # Strip all old tracking metadata
        "-metadata", "title=iPhone 15 Pro Max",
        "-metadata", "artist=Apple",
        "-metadata", "comment=iOS 17.5.1",
        "-metadata", "encoder=Apple QuickTime",
        "-metadata", "handler_name=Core Media Video",
        "-metadata", f"creation_time={recent_time}",
    ]

    if with_audio:
        cmd.extend([
            "-af", f"atempo={speed},volume=1.01",
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
            timeout=1800  # 30 min max
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
