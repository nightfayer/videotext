"""
ai_assistant.py - AI-powered content analysis, summary, and post generator using NVIDIA NIM models.
"""

import asyncio
import logging
import aiohttp
from typing import Tuple
import config

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """Ты — профессиональный контент-мейкер, SMM-стратег и редактор.
Твоя задача — внимательно проанализировать расшифровку речи из аудио/видео и составить:

1. 📌 **Главные тезисы** (3-5 ключевых мыслей или выводов списком).
2. 💡 **Готовый пост для Telegram/Instagram** (с вовлекающим заголовком, структурированным текстом и гармоничными эмодзи).
3. 🏷️ **Релевантные хэштеги** (для продвижения и охватов).

Отвечай на языке исходного текста, структурированно, без воды и лишних предисловий. Используй красивую разметку Markdown.
"""

async def generate_ai_summary(
    text: str,
    custom_prompt: str = None,
    max_tokens: int = 1500,
    temperature: float = 0.3
) -> Tuple[bool, str]:
    """
    Generates structured AI summary, post, and hashtags from transcription text
    using NVIDIA NIM API.
    """
    if not config.NVIDIA_API_KEY:
        return False, "⚠️ API-ключ NVIDIA не настроен в файле `.env` (параметр `NVIDIA_API_KEY`)."

    clean_text = text.strip()
    if not clean_text:
        return False, "⚠️ Текст для анализа пуст."

    # Prevent token overflow: truncate extremely large transcripts (~15,000 chars ≈ 3,500-4,000 words)
    if len(clean_text) > 15000:
        clean_text = clean_text[:15000] + "\n\n[...Текст сокращен для соблюдения лимита контекста...]"

    system_prompt = custom_prompt or DEFAULT_SYSTEM_PROMPT
    url = f"{config.NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "model": config.NVIDIA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Вот расшифровка речи:\n\n{clean_text}"}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    timeout = aiohttp.ClientTimeout(total=90)
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                post_kwargs = {"headers": headers, "json": payload}
                if config.AI_PROXY:
                    post_kwargs["proxy"] = config.AI_PROXY

                async with session.post(url, **post_kwargs) as resp:
                    if resp.status == 451:
                        err_body = await resp.text()
                        logger.error(f"[NVIDIA API Geo-Block 451]: {err_body}")
                        return False, (
                            "⛔ Ошибка 451 (Геоблокировка NVIDIA):\n"
                            "IP-адрес вашего сервера заблокирован NVIDIA по региональному признаку (РФ).\n\n"
                            "💡 Как исправить:\n"
                            "1. Укажите зарубежный прокси в `.env` на сервере: `AI_PROXY=http://логин:пароль@ip:порт` (или socks5://)\n"
                            "2. Либо используйте сервер за пределами РФ."
                        )

                    if resp.status in (429, 502, 503, 504) and attempt < max_retries:
                        logger.warning(f"[NVIDIA API Busy {resp.status}], attempt {attempt}/{max_retries}. Retrying in 2s...")
                        await asyncio.sleep(2.0 * attempt)
                        continue

                    if resp.status != 200:
                        err_body = await resp.text()
                        logger.error(f"[NVIDIA API Error {resp.status}]: {err_body}")
                        return False, f"Ошибка NVIDIA API ({resp.status}): {err_body[:150]}"

                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return False, "Нейросеть вернула пустой ответ."

                    result_text = choices[0].get("message", {}).get("content", "").strip()
                    if not result_text:
                        return False, "Получен пустой текст ответа от модели."

                    return True, result_text

        except (aiohttp.ClientConnectorError, TimeoutError) as e:
            if attempt < max_retries:
                logger.warning(f"Connection issue ({e}), attempt {attempt}/{max_retries}. Retrying in 2s...")
                await asyncio.sleep(2.0 * attempt)
                continue
            return False, f"Ошибка подключения к серверам NVIDIA: {e}"
        except Exception as e:
            logger.error(f"Unexpected error in generate_ai_summary: {e}", exc_info=True)
            return False, f"Непредвиденная ошибка при запросе к AI: {str(e)}"

    return False, "Серверы NVIDIA временно перегружены. Попробуйте снова через минуту."
