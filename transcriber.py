import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, List

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from faster_whisper import WhisperModel
import config
from audio_utils import format_duration


def srt_timestamp(seconds: float) -> str:
    """Formats seconds into SRT timestamp format: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


class WhisperTranscriber:
    def __init__(self):
        self.model = None
        self.model_name = config.WHISPER_MODEL
        self.device = config.DEVICE
        self.compute_type = config.COMPUTE_TYPE
        self.last_used_time = 0.0

    def ensure_model_loaded(self):
        if self.model is not None:
            return
        self._load_model()

    def unload_model(self):
        """Unloads Whisper model from memory to free RAM when idle."""
        if self.model is not None:
            print(f"[Whisper] 💤 Модель '{self.model_name}' выгружена из памяти из-за простоя (освобождено ~1.2 ГБ RAM)", flush=True)
            self.model = None
            import gc
            gc.collect()
            if self.device == "cuda":
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass

    async def monitor_idle(self, idle_timeout: int = 600, check_interval: int = 60):
        """Monitors model inactivity and unloads it after idle_timeout seconds (default 10 min)."""
        while True:
            try:
                await asyncio.sleep(check_interval)
                if self.model is not None and self.last_used_time > 0:
                    if time.time() - self.last_used_time >= idle_timeout:
                        self.unload_model()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Whisper Idle Monitor Error]: {e}", flush=True)

    def _load_model(self):
        print(f"[Whisper] Загрузка модели '{self.model_name}' на {self.device} ({self.compute_type})...")
        try:
            self.model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type
            )
            print(f"[Whisper] Модель '{self.model_name}' успешно загружена на {self.device}!")
        except Exception as e:
            if self.device == "cuda":
                print(f"[Whisper Warning] Не удалось запустить на CUDA ({e}). Переключение на CPU (int8)...")
                self.device = "cpu"
                self.compute_type = "int8"
                self.model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8"
                )
                print(f"[Whisper] Модель '{self.model_name}' успешно запущена на CPU!")
            else:
                raise e

    def _sync_transcribe(self, audio_path: Path, language: str = None) -> Dict[str, Any]:
        self.ensure_model_loaded()
        self.last_used_time = time.time()
        start_time = time.time()
        target_lang = language or config.WHISPER_LANGUAGE or None

        try:
            segments, info = self.model.transcribe(
                str(audio_path),
                language=target_lang,
                beam_size=config.BEAM_SIZE,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=400,
                    threshold=0.5
                )
            )

            segment_list = []
            plain_text_parts = []
            timestamped_parts = []
            srt_lines = []

            for idx, seg in enumerate(segments, start=1):
                text = seg.text.strip()
                if not text:
                    continue

                start_fmt = format_duration(seg.start)
                end_fmt = format_duration(seg.end)

                segment_list.append({
                    "id": idx,
                    "start": seg.start,
                    "end": seg.end,
                    "text": text
                })

                plain_text_parts.append(text)
                timestamped_parts.append(f"[{start_fmt} - {end_fmt}] {text}")
                srt_lines.append(f"{idx}\n{srt_timestamp(seg.start)} --> {srt_timestamp(seg.end)}\n{text}\n")

        except Exception as e:
            if self.device == "cuda":
                print(f"[Whisper Warning] Сбой выполнения на CUDA ({e}). Повтор на CPU (int8)...", flush=True)
                self.device = "cpu"
                self.compute_type = "int8"
                self.model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8"
                )
                return self._sync_transcribe(audio_path, language)
            raise e

        duration_taken = time.time() - start_time

        full_text = " ".join(plain_text_parts).strip()
        timestamped_text = "\n".join(timestamped_parts).strip()
        srt_text = "\n".join(srt_lines).strip()

        return {
            "text": full_text,
            "timestamped_text": timestamped_text,
            "srt": srt_text,
            "segments_count": len(segment_list),
            "language": info.language,
            "language_probability": info.language_probability,
            "audio_duration": info.duration,
            "processing_time": duration_taken
        }

    async def transcribe(self, audio_path: Path, language: str = None) -> Dict[str, Any]:
        """Runs transcription asynchronously in thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_transcribe, audio_path, language)


# Global instance
transcriber = WhisperTranscriber()
