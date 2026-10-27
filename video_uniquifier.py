import asyncio
import datetime
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from audio_utils import get_ffmpeg_executable

_NVENC_AVAILABLE = None

DEVICE_PROFILES = [
    {
        "model": "iPhone 15 Pro Max",
        "make": "Apple",
        "comment": "iOS 17.5.1",
        "encoder": "Apple QuickTime",
        "handler": "Core Media Video",
    },
    {
        "model": "iPhone 16 Pro",
        "make": "Apple",
        "comment": "iOS 18.1",
        "encoder": "Apple QuickTime",
        "handler": "Core Media Video",
    },
    {
        "model": "Samsung Galaxy S24 Ultra",
        "make": "Samsung",
        "comment": "Android 14 OneUI 6.1 SM-S928B",
        "encoder": "Samsung Camera",
        "handler": "VideoHandle",
    },
    {
        "model": "Google Pixel 8 Pro",
        "make": "Google",
        "comment": "Android 14 build AP2A.240805.005",
        "encoder": "Google Camera HDR+",
        "handler": "VideoHandle",
    },
    {
        "model": "Sony Alpha 7 IV",
        "make": "Sony Corporation",
        "comment": "ILCE-7M4 Ver. 3.00 XAVC S 4K",
        "encoder": "Sony XAVC S",
        "handler": "Sony Video Media Handler",
    },
    {
        "model": "GoPro HERO12 Black",
        "make": "GoPro",
        "comment": "HD12.01.02.00.00 GP2",
        "encoder": "GoPro MP4 v2",
        "handler": "GoPro Video",
    },
]


def is_nvenc_available() -> bool:
    """Checks whether NVIDIA NVENC hardware acceleration is functional."""
    global _NVENC_AVAILABLE
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE
    ffmpeg_bin = get_ffmpeg_executable()
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-f", "lavfi", "-i", "nullsrc=s=640x360:d=0.05", "-c:v", "h264_nvenc", "-f", "null", "-"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5
        )
        _NVENC_AVAILABLE = (res.returncode == 0)
    except Exception:
        _NVENC_AVAILABLE = False
    return _NVENC_AVAILABLE


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


def build_uniquify_command(
    input_file: Path,
    output_file: Path,
    flip_horizontal: bool = False
) -> tuple[list[str], float, dict]:
    """
    Constructs the FFmpeg command line with randomized parameters, hardware acceleration,
    anti-detect timeline micro-trim, and progress reporting.
    Returns (cmd_list, total_duration, metadata_info).
    """
    ffmpeg_bin = get_ffmpeg_executable()
    total_duration = get_media_duration(input_file)

    # 1. Randomized visual parameters
    zoom = round(random.uniform(1.020, 1.028), 3)
    angle_deg = round(random.choice([-1, 1]) * random.uniform(0.30, 0.50), 2)
    speed = round(random.uniform(1.006, 1.014), 3)
    contrast = round(random.uniform(1.010, 1.022), 3)
    saturation = round(random.uniform(1.010, 1.022), 3)
    brightness = round(random.uniform(0.003, 0.007), 3)
    noise_seed = random.randint(1000, 999999)
    vignette_div = random.randint(140, 180)

    # 2. Micro-trim start (0.12 - 0.22 sec) if video is long enough
    trim_start = 0.0
    if total_duration >= 3.0:
        trim_start = round(random.uniform(0.12, 0.22), 2)

    vf_filters = []
    if trim_start > 0:
        vf_filters.extend([f"trim=start={trim_start}", "setpts=PTS-STARTPTS"])

    if flip_horizontal:
        vf_filters.append("hflip")

    vf_filters.extend([
        f"scale=ceil(iw*{zoom}/2)*2:ceil(ih*{zoom}/2)*2",
        f"rotate={angle_deg}*PI/180:ow=iw:oh=ih",
        "crop=iw:ih",
        f"eq=contrast={contrast}:brightness={brightness}:saturation={saturation}",
        f"noise=alls=2:allf=t:all_seed={noise_seed}",
        f"vignette=PI/{vignette_div}",
        f"setpts=PTS/{speed}"
    ])
    vf_str = ",".join(vf_filters)

    with_audio = has_audio_stream(input_file)

    # 3. Device Spoofing & creation time
    profile = random.choice(DEVICE_PROFILES)
    recent_time = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=random.randint(12, 35))
    ).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    use_nvenc = is_nvenc_available()
    encoder_name = "NVIDIA NVENC (GPU ⚡)" if use_nvenc else "libx264 (CPU 💻)"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-threads", "0",
        "-i", str(input_file),
        "-vf", vf_str,
    ]

    if use_nvenc:
        cmd.extend([
            "-c:v", "h264_nvenc",
            "-cq", "19",
            "-preset", "p4",
        ])
    else:
        cmd.extend([
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast",
        ])

    cmd.extend([
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-map_metadata", "-1",
        "-metadata", f"title={profile['model']}",
        "-metadata", f"artist={profile['make']}",
        "-metadata", f"comment={profile['comment']}",
        "-metadata", f"encoder={profile['encoder']}",
        "-metadata", f"handler_name={profile['handler']}",
        "-metadata", f"creation_time={recent_time}",
        "-progress", "pipe:1",
        "-nostats",
    ])

    if with_audio:
        eq_freq = random.randint(900, 2400)
        eq_gain = round(random.uniform(0.3, 0.5), 2)
        af_filters = []
        if trim_start > 0:
            af_filters.extend([f"atrim=start={trim_start}", "asetpts=PTS-STARTPTS"])
        af_filters.extend([
            f"equalizer=f={eq_freq}:t=q:w=1:g={eq_gain}",
            f"atempo={speed}",
            "volume=1.01"
        ])
        af_str = ",".join(af_filters)
        cmd.extend([
            "-af", af_str,
            "-c:a", "aac",
            "-b:a", "192k"
        ])
    else:
        cmd.append("-an")

    cmd.append(str(output_file))

    meta_info = {
        "device": f"{profile['make']} {profile['model']}",
        "encoder": encoder_name,
        "flip": flip_horizontal,
        "trim": trim_start,
        "zoom": f"+{round((zoom - 1.0) * 100, 1)}%",
        "speed": f"{round((speed - 1.0) * 100, 1)}%",
    }

    effective_duration = max(0.0, total_duration - trim_start) / speed if total_duration > 0 else 0.0
    return cmd, effective_duration, meta_info


def check_needs_normalization(input_file: Path) -> bool:
    """Checks if the video codec is likely to cause FFmpeg issues."""
    ffmpeg_bin = get_ffmpeg_executable()
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-i", str(input_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        output = res.stderr.decode("utf-8", errors="ignore").lower()
        # HEVC (h265), ProRes, 10-bit color are common hang causes
        problematic = ["hevc", "prores", "10bit", "heic"]
        return any(p in output for p in problematic)
    except Exception:
        return True # Default to normalization if check fails


def normalize_video(input_file: Path) -> Path:
    """Converts a potentially problematic video to a standard h264/yuv420p file."""
    output_file = input_file.parent / f"{input_file.stem}_norm.mp4"
    ffmpeg_bin = get_ffmpeg_executable()
    print(f"[FFmpeg] Normalizing {input_file.name} to standard H.264...", flush=True)
    
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(input_file),
        "-c:v", "libx264",
        "-profile:v", "baseline",
        "-level", "3.0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_file)
    ]
    
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return output_file


async def async_uniquify_video(
    input_file: Path,
    output_file: Path,
    progress_callback=None,
    cancel_event: asyncio.Event = None,
    flip_horizontal: bool = False
) -> tuple[bool, str, dict]:
    """
    Asynchronously runs video uniquification with real-time progress callbacks and cancellation support.
    Returns (success, error_message, metadata_dict).
    """
    if not input_file.exists():
        return False, "Входной файл не найден на сервере.", {}
    if input_file.stat().st_size == 0:
        return False, "Входной файл пуст (0 байт).", {}

    # NEW: Normalization step
    working_file = input_file
    normalized = False
    try:
        if check_needs_normalization(input_file):
            working_file = normalize_video(input_file)
            normalized = True
    except Exception as e:
        print(f"[Normalization Error]: {e}", flush=True)

    cmd, total_duration, meta_info = build_uniquify_command(
        working_file,
        output_file,
        flip_horizontal=flip_horizontal
    )

    # Запускаем FFmpeg с логированием вывода в stdout
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        print(f"[FFmpeg] Command started: {' '.join(cmd[:10])}...", flush=True)
    except Exception as e:
        if normalized:
            working_file.unlink()
        return False, f"Не удалось запустить FFmpeg: {e}", {}


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
        
        # CLEANUP: remove normalized file if created
        if normalized:
            working_file.unlink()

        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError()

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read()
            err_msg = stderr_bytes.decode("utf-8", errors="ignore").strip()
            print(f"[Uniquifier FFmpeg Error]: {err_msg}", flush=True)
            return False, f"Ошибка FFmpeg при обработке: {err_msg[:150]}", {}

        if not output_file.exists() or output_file.stat().st_size == 0:
            return False, "Уникализированный файл не был создан или пуст.", {}

        # Final 100% callback
        if progress_callback and total_duration > 0:
            try:
                await progress_callback(total_duration, total_duration, speed_str)
            except Exception:
                pass

        return True, "", meta_info

    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    except Exception as e:
        return False, f"Ошибка при уникализации видео: {str(e)}", {}
