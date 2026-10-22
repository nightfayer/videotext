import asyncio
import datetime
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from audio_utils import get_ffmpeg_executable


def get_media_duration(input_file: Path) -> float:
    """Extracts media duration in seconds via FFmpeg probe."""
    ffmpeg_bin = get_ffmpeg_executable()
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-i", str(input_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10
        )
        output = res.stderr.decode("utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = float(match.group(3))
            return hours * 3600 + minutes * 60 + seconds
    except Exception as e:
        print(f"[Duration Probe Warning]: {e}", flush=True)
    return 0.0


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


def build_uniquify_command(input_file: Path, output_file: Path) -> tuple[list[str], float]:
    """
    Constructs the FFmpeg command line with randomized parameters and progress reporting.
    Returns (cmd_list, total_duration).
    """
    ffmpeg_bin = get_ffmpeg_executable()
    total_duration = get_media_duration(input_file)

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
        "-progress", "pipe:1",    # Output real-time progress to stdout
        "-nostats",
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
    return cmd, total_duration


async def async_uniquify_video(
    input_file: Path,
    output_file: Path,
    progress_callback=None,
    cancel_event: asyncio.Event = None
) -> tuple[bool, str]:
    """
    Asynchronously runs video uniquification with real-time progress callbacks and cancellation support.
    """
    if not input_file.exists():
        return False, "Входной файл не найден на сервере."
    if input_file.stat().st_size == 0:
        return False, "Входной файл пуст (0 байт)."

    cmd, total_duration = build_uniquify_command(input_file, output_file)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    except Exception as e:
        return False, f"Не удалось запустить FFmpeg: {e}"

    speed_str = ""
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                raise asyncio.CancelledError("Uniquification cancelled by user")

            line = await proc.stdout.readline()
            if not line:
                break

            text = line.decode("utf-8", errors="ignore").strip()
            if text.startswith("speed="):
                speed_str = text.split("=")[1].strip()
            elif text.startswith("out_time_us="):
                us_val = text.split("=")[1].strip()
                if us_val.isdigit() and total_duration > 0:
                    current_sec = int(us_val) / 1_000_000
                    if progress_callback:
                        try:
                            await progress_callback(current_sec, total_duration, speed_str)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            pass

        await proc.wait()

        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError()

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read()
            err_msg = stderr_bytes.decode("utf-8", errors="ignore").strip()
            print(f"[Uniquifier FFmpeg Error]: {err_msg}", flush=True)
            return False, f"Ошибка FFmpeg при обработке: {err_msg[:150]}"

        if not output_file.exists() or output_file.stat().st_size == 0:
            return False, "Уникализированный файл не был создан или пуст."

        # Final 100% callback
        if progress_callback and total_duration > 0:
            try:
                await progress_callback(total_duration, total_duration, speed_str)
            except Exception:
                pass

        return True, ""

    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    except Exception as e:
        return False, f"Ошибка при уникализации видео: {str(e)}"
