import asyncio
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from telethon import TelegramClient, events, Button, functions, types
from telethon.tl.types import (
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
)

from telethon.sessions import MemorySession

import config
from audio_utils import (
    format_duration,
    format_bytes,
    async_extract_audio,
    safe_remove,
    sanitize_filename,
    create_docx_report,
    periodic_temp_cleanup,
)
from fast_download import fast_download_file
from transcriber import transcriber
from video_uniquifier import async_uniquify_video
from post_formatter import generate_telegram_post

# Использование MemorySession исключает блокировки файлов SQLite (database is locked) в Docker
client = TelegramClient(
    MemorySession(),
    config.API_ID or 1,
    config.API_HASH or "none",
    use_ipv6=False
)

class TaskQueueTracker:
    """Manages concurrent processing tasks and notifies users of their queue position."""
    def __init__(self, max_concurrent: int):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.waiting = []
        self.lock = asyncio.Lock()

    async def acquire(self, status_msg):
        async with self.lock:
            self.waiting.append(status_msg)
            pos = len(self.waiting)

        if pos > 1:
            try:
                await status_msg.edit(f"⏳ **В очереди:** позиция `{pos}`\nОбработка начнётся автоматически, как только освободится сервер...")
            except Exception:
                pass

        await self.semaphore.acquire()

        async with self.lock:
            if status_msg in self.waiting:
                self.waiting.remove(status_msg)
            for idx, msg in enumerate(self.waiting, start=1):
                try:
                    await msg.edit(f"⏳ **В очереди:** позиция `{idx}`\nОбработка начнётся автоматически...")
                except Exception:
                    pass

    def release(self):
        self.semaphore.release()

task_queue = TaskQueueTracker(config.MAX_CONCURRENT_TASKS)

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".3gp", ".m4v", ".ts",
    ".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac", ".wma"
}

LANGUAGE_NAMES = {
    "ru": "Русский",
    "en": "Английский",
    "uk": "Украинский",
    "be": "Белорусский",
    "kk": "Казахский",
    "uz": "Узбекский",
    "de": "Немецкий",
    "fr": "Французский",
    "es": "Испанский",
    "it": "Итальянский",
    "zh": "Китайский",
    "ja": "Японский",
    "ko": "Корейский",
    "tr": "Турецкий",
    "ar": "Арабский",
    "pt": "Португальский",
    "pl": "Польский",
}


def is_media_message(message) -> bool:
    if message.video or message.video_note or message.audio or message.voice:
        return True
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("video/") or mime.startswith("audio/"):
            return True
        for attr in message.document.attributes:
            if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAudio)):
                return True
            if isinstance(attr, DocumentAttributeFilename):
                ext = Path(attr.file_name).suffix.lower()
                if ext in MEDIA_EXTENSIONS:
                    return True
    return False


def get_media_filename(message) -> str:
    if message.file and message.file.name:
        raw_name = Path(message.file.name).stem
        return sanitize_filename(raw_name)
    if message.video_note:
        return f"circle_{int(time.time())}"
    if message.voice:
        return f"voice_{int(time.time())}"
    if message.video:
        return f"video_{int(time.time())}"
    if message.audio:
        return f"audio_{int(time.time())}"
    return f"media_{int(time.time())}"


class ThrottledProgress:
    def __init__(self, message, action_title="Скачивание", buttons=None, cancel_event=None):
        self.message = message
        self.action_title = action_title
        self.buttons = buttons
        self.cancel_event = cancel_event
        self.last_update_time = 0.0
        self.last_percent = -1

    async def callback(self, current: int, total: int):
        if self.cancel_event and self.cancel_event.is_set():
            raise asyncio.CancelledError("Download cancelled by user")
        if not total:
            return
        percent = int((current / total) * 100)
        now = time.time()

        if (now - self.last_update_time >= 3.0) or (percent - self.last_percent >= 15) or (percent == 100):
            self.last_update_time = now
            self.last_percent = percent
            progress_bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            text = (
                f"📥 **{self.action_title}**: `{percent}%`\n"
                f"`[{progress_bar}]`\n"
                f"💾 {format_bytes(current)} / {format_bytes(total)}"
            )
            try:
                await self.message.edit(text, buttons=self.buttons)
            except Exception:
                pass


class ThrottledUniquifyProgress:
    def __init__(self, message, action_title="Уникализация видео", buttons=None, cancel_event=None):
        self.message = message
        self.action_title = action_title
        self.buttons = buttons
        self.cancel_event = cancel_event
        self.last_update_time = 0.0
        self.last_percent = -1

    async def callback(self, current_sec: float, total_sec: float, speed_str: str = ""):
        if self.cancel_event and self.cancel_event.is_set():
            raise asyncio.CancelledError("Uniquification cancelled by user")
        if not total_sec or total_sec <= 0:
            return

        percent = min(100, int((current_sec / total_sec) * 100))
        now = time.time()

        # Throttle edits to at least 2.5s or 10% step or 100% completion
        if (now - self.last_update_time >= 2.5) or (percent - self.last_percent >= 10) or (percent == 100):
            self.last_update_time = now
            self.last_percent = percent
            progress_bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))

            cur_min, cur_s = divmod(int(current_sec), 60)
            tot_min, tot_s = divmod(int(total_sec), 60)
            time_str = f"{cur_min:02d}:{cur_s:02d} / {tot_min:02d}:{tot_s:02d}"

            speed_info = f" • Скорость: `{speed_str}`" if speed_str else ""
            text = (
                f"✨ **{self.action_title}**: `{percent}%`\n"
                f"`[{progress_bar}]`\n"
                f"⏱️ `{time_str}`{speed_info}\n"
                f"⚙️ Микро-зум, перцептивный шум, цветокоррекция, метаданные"
            )
            try:
                await self.message.edit(text, buttons=self.buttons)
            except Exception:
                pass


active_tasks = {}
user_modes = {}  # sender_id -> 'transcribe' | 'uniqueize' | 'post'
user_flip = {}   # sender_id -> bool (horizontal flip mirroring)


def get_mode_keyboard():
    return [
        [
            Button.text("🎙️ Видео в Текст", resize=True),
            Button.text("✨ Уникализировать видео", resize=True)
        ],
        [
            Button.text("✍️ Написать ТГ-пост", resize=True),
            Button.text("🚀 Меню / Старт", resize=True)
        ]
    ]


def check_access(sender_id: int) -> bool:
    if not config.ALLOWED_USERS:
        return True
    return sender_id in config.ALLOWED_USERS


@client.on(events.NewMessage(incoming=True, pattern=r'^(/start|🚀 Меню / Старт)$'))
async def start_handler(event):
    if not check_access(event.sender_id):
        await event.respond("⛔ **Доступ ограничен.**\nВы не авторизованы для использования этого бота.")
        return

    user_modes[event.sender_id] = "transcribe"
    welcome_text = (
        "👋 **Привет! Я многофункциональный бот для контента.**\n\n"
        "⚡ **Выберите нужный режим кнопками ниже:**\n\n"
        "1️⃣ **🎙️ Видео в Текст:**\n"
        "• Распознавание речи из видео, кружочков, аудио и голосовых\n"
        "• Создание субтитров `.srt`, текста с таймкодами и документа Word `.docx`\n\n"
        "2️⃣ **✨ Уникализировать видео:**\n"
        "• Подготовка видео для **Instagram Reels, TikTok и Shorts**\n"
        "• Уникализация **без потери качества** (микро-зум, перцептивный шум, цветовой сдвиг, смена аудиохэша, очистка метаданных)\n"
        "• Позволяет обойти фильтры повторного контента и теневой бан\n\n"
        "3️⃣ **✍️ Написать ТГ-пост:**\n"
        "• Превращение любых ваших мыслей, черновиков, заметок или расшифровок в профессионально оформленный пост для Telegram\n"
        "• Цепляющий заголовок, четкая структура с акцентами, цитаты и хэштеги\n\n"
        "📦 Поддержка файлов до **2 ГБ**!"
    )
    await event.respond(welcome_text, buttons=get_mode_keyboard())


@client.on(events.NewMessage(incoming=True, pattern=r'^🎙️ Видео в Текст$'))
async def mode_transcribe_handler(event):
    if not check_access(event.sender_id):
        return
    user_modes[event.sender_id] = "transcribe"
    await event.respond(
        "🎙️ **Включен режим: Видео в Текст**\n\n"
        "Отправьте мне видео, кружочек, аудиофайл или голосовое сообщение — я переведу речь в текст, создам файл субтитров .srt и документ Word!",
        buttons=get_mode_keyboard()
    )


@client.on(events.NewMessage(incoming=True, pattern=r'^✨ Уникализировать видео$'))
async def mode_uniqueize_handler(event):
    if not check_access(event.sender_id):
        return
    user_modes[event.sender_id] = "uniqueize"
    flip_status = "ВКЛЮЧЕНО ✅" if user_flip.get(event.sender_id, False) else "ВЫКЛЮЧЕНО ❌"
    await event.respond(
        "✨ **Включен режим: Уникализация видео**\n\n"
        "Отправьте мне видео или кружочек — я уникализирую его без потери качества для **Instagram Reels, TikTok и Shorts**.\n\n"
        "🛡️ **Комплекс анти-детект защиты:**\n"
        "• 📱 Спуфинг мобильных камер (Apple iPhone 16 Pro, Samsung S24 Ultra, Pixel 8)\n"
        "• ✂️ Микро-срезка первых 0.15 сек (ломает совпадение первого ключевого кадра)\n"
        "• 🔍 Микро-зум и перцептивный шум на каждом кадре\n"
        "• 🖼️ Мягкая виньетка и цветокоррекция\n"
        "• 🎵 Микро-эквалайзер звука и акустический сдвиг Sound ID\n"
        "• ⚡ Аппаратное ускорение NVIDIA NVENC / CPU\n\n"
        f"🪞 **Зеркальное отражение (Flip):** `{flip_status}`\n"
        "💡 _Отправьте команду /flip для включения или выключения зеркала._",
        buttons=get_mode_keyboard()
    )


@client.on(events.NewMessage(incoming=True, pattern=r'^/flip$'))
async def flip_toggle_handler(event):
    if not check_access(event.sender_id):
        return
    current = user_flip.get(event.sender_id, False)
    new_val = not current
    user_flip[event.sender_id] = new_val
    state_str = "ВКЛЮЧЕНО ✅" if new_val else "ВЫКЛЮЧЕНО ❌"
    tip = (
        "Видео будет отражено по горизонтали (зеркальный эффект для максимального обхода алгоритмов соцсетей)."
        if new_val
        else "Видео будет обработано в стандартной ориентации (без зеркального отражения)."
    )
    await event.respond(
        f"🪞 **Зеркальное отражение видео:** `{state_str}`\n\n{tip}",
        buttons=get_mode_keyboard()
    )


@client.on(events.NewMessage(incoming=True, pattern=r'^✍️ Написать ТГ-пост$'))
async def mode_post_handler(event):
    if not check_access(event.sender_id):
        return
    user_modes[event.sender_id] = "post"
    await event.respond(
        "✍️ **Включен режим: Оформление ТГ-постов**\n\n"
        "Пришлите мне любой черновик, мысли, статью или расшифровку — я красиво оформлю их в готовый, стильный пост для Telegram с цепляющим заголовком, структурой, цитатами и хэштегами!",
        buttons=get_mode_keyboard()
    )


@client.on(events.NewMessage(incoming=True, pattern=r'^/help$'))
async def help_handler(event):
    if not check_access(event.sender_id):
        await event.respond("⛔ **Доступ ограничен.**")
        return

    help_text = (
        "📖 **Инструкция по использованию:**\n\n"
        "1️⃣ **🎙️ Режим «Видео в Текст»:**\n"
        "Отправьте видео, кружочек, аудиофайл или голосовое. Бот расшифрует речь, пришлет текст, субтитры `.srt` и документ Word `.docx`.\n\n"
        "2️⃣ **✨ Режим «Уникализировать видео»:**\n"
        "Отправьте видеоролик. Бот применит микро-зум, перцептивный шум и очистит метаданные для безопасной публикации в Reels, TikTok и Shorts.\n\n"
        "3️⃣ **✍️ Режим «Написать ТГ-пост»:**\n"
        "Отправьте черновик, мысли или расшифровку. Бот оформит их в красивый готовый Telegram-пост с заголовком, буллетами, цитатами и хэштегами.\n\n"
        f"⚙️ **Текущая конфигурация:**\n"
        f"• Режим: `Telethon (до 2 ГБ)`\n"
        f"• Whisper: `{config.WHISPER_MODEL}` | Ускоритель: `{transcriber.device.upper()}`\n"
        f"• AI-редактор: `{config.NVIDIA_MODEL}`\n"
    )
    await event.respond(help_text)


@client.on(events.NewMessage(incoming=True, pattern=r'^/settings$'))
async def settings_handler(event):
    if not check_access(event.sender_id):
        await event.respond("⛔ **Доступ ограничен.**")
        return

    settings_text = (
        "⚙️ **Настройки бота:**\n\n"
        f"• **Режим:** `Telethon (файлы до 2 ГБ)`\n"
        f"• **Whisper модель:** `{config.WHISPER_MODEL}`\n"
        f"• **Устройство:** `{transcriber.device.upper()}`\n"
        f"• **Compute Type:** `{transcriber.compute_type}`\n"
        f"• **Язык распознавания:** `{config.WHISPER_LANGUAGE or 'Автоопределение'}`\n"
        f"• **Поиск гипотез (beam_size):** `{config.BEAM_SIZE}`\n"
        f"• **Одновременных задач:** `{config.MAX_CONCURRENT_TASKS}`\n\n"
        "💡 Для изменения параметров отредактируйте файл `.env` и перезапустите бота."
    )
    await event.respond(settings_text)


@client.on(events.CallbackQuery(pattern=r'^cancel_(.+)'))
async def cancel_callback_handler(event):
    data_str = event.data.decode("utf-8")
    task_id = data_str.removeprefix("cancel_")

    task_info = active_tasks.get(task_id)
    if not task_info:
        await event.answer("⚠️ Задача уже завершена или не найдена.", alert=True)
        try:
            await event.edit("🚫 **Задача завершена или отменена.**", buttons=None)
        except Exception:
            pass
        return

    # Check access: only original sender or allowed users can cancel
    if event.sender_id != task_info["sender_id"] and not check_access(event.sender_id):
        await event.answer("⛔ Вы не можете отменить чужую задачу.", alert=True)
        return

    task_info["cancel_event"].set()
    task = task_info.get("task")
    if task and not task.done():
        task.cancel()

    await event.answer("Загрузка отменяется...", alert=False)
    try:
        await event.edit("🚫 **Скачивание отменено пользователем.**", buttons=None)
    except Exception:
        pass


@client.on(events.NewMessage(incoming=True, pattern=r'^/post(\s+.*)?$'))
async def post_cmd_handler(event):
    if not check_access(event.sender_id):
        return

    raw_text = ""
    if event.pattern_match.group(1) and event.pattern_match.group(1).strip():
        raw_text = event.pattern_match.group(1).strip()
    elif event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg and reply_msg.text:
            raw_text = reply_msg.text

    if not raw_text:
        await event.respond(
            "✍️ **Как оформить ТГ-пост:**\n\n"
            "• Либо переключитесь кнопкой в режим **✍️ Написать ТГ-пост** и пришлите текст сообщением.\n"
            "• Либо отправьте: `/post <ваш черновик>`\n"
            "• Либо ответьте командой `/post` на любое сообщение с текстом.",
            buttons=get_mode_keyboard()
        )
        return

    await process_and_send_post(event, raw_text)


@client.on(events.NewMessage(incoming=True, func=lambda e: not is_media_message(e) and not (e.text or "").startswith('/')))
async def text_handler(event):
    if not check_access(event.sender_id):
        return

    text = (event.text or "").strip()
    if not text:
        return

    if text in ["🎙️ Видео в Текст", "✨ Уникализировать видео", "✍️ Написать ТГ-пост", "🚀 Меню / Старт"]:
        return

    current_mode = user_modes.get(event.sender_id, "transcribe")
    if current_mode == "post":
        await process_and_send_post(event, text)
    elif current_mode == "uniqueize":
        await event.respond(
            "⚠️ **Включен режим: Уникализация видео.**\n\n"
            "Пожалуйста, пришлите видеофайл или кружочек.\n"
            "Чтобы красиво оформить текст в пост, переключитесь кнопкой на **✍️ Написать ТГ-пост**.",
            buttons=get_mode_keyboard()
        )
    else:
        await event.respond(
            "🎙️ **Включен режим: Видео в Текст.**\n\n"
            "Пришлите видео, кружочек или аудиофайл для расшифровки речи.\n"
            "Чтобы красиво оформить текст в пост, переключитесь кнопкой на **✍️ Написать ТГ-пост**.",
            buttons=get_mode_keyboard()
        )


async def process_and_send_post(event, raw_text: str):
    progress_msg = await event.respond(
        "✍️ **Оформляю красивый пост для Telegram...**\n"
        f"Модель: `{config.NVIDIA_MODEL}` ⚡"
    )
    success, html_post, err = await generate_telegram_post(raw_text)
    if not success:
        await progress_msg.edit(f"❌ {err or 'Не удалось оформить пост.'}")
        return

    if len(html_post) <= 4000:
        try:
            await progress_msg.edit(html_post, parse_mode="html")
        except Exception:
            try:
                await progress_msg.edit(html_post, parse_mode=None)
            except Exception:
                pass
    else:
        part1 = html_post[:3800]
        part2 = html_post[3800:]
        try:
            await progress_msg.edit(part1, parse_mode="html")
            await event.respond(part2, parse_mode="html")
        except Exception:
            await progress_msg.edit(part1, parse_mode=None)
            await event.respond(part2, parse_mode=None)


def get_media_ext(message) -> str:
    if message.video:
        return ".mp4"
    if message.video_note:
        return ".mp4"
    if message.voice:
        return ".ogg"
    if message.audio:
        if message.file and message.file.name:
            return Path(message.file.name).suffix or ".mp3"
        return ".mp3"
    if message.document and message.file and message.file.name:
        return Path(message.file.name).suffix or ".mp4"
    return ".mp4"


@client.on(events.NewMessage(incoming=True, func=is_media_message))
async def media_handler(event):
    if not check_access(event.sender_id):
        await event.reply("⛔ **Доступ ограничен.**\nВы не авторизованы для использования этого бота.")
        return

    message = event.message
    file_size = message.file.size if message.file else 0

    status_msg = await event.reply("⏳ Добавлено в очередь на обработку...")

    await task_queue.acquire(status_msg)
    try:
        base_name = get_media_filename(message)
        ext = get_media_ext(message)
        timestamp_id = int(time.time() * 1000)
        unique_prefix = f"{timestamp_id}_{message.sender_id}"
        task_id = f"{message.chat_id}_{timestamp_id}"

        download_path = config.TEMP_DIR / f"{unique_prefix}_input{ext}"
        audio_wav_path = config.TEMP_DIR / f"{unique_prefix}_audio.wav"
        txt_path = config.TEMP_DIR / f"{base_name}.txt"
        srt_path = config.TEMP_DIR / f"{base_name}.srt"
        docx_path = config.TEMP_DIR / f"{base_name}.docx"
        unique_video_path = config.TEMP_DIR / f"{unique_prefix}_unique.mp4"

        cancel_event = asyncio.Event()
        current_task = asyncio.current_task()
        active_tasks[task_id] = {
            "cancel_event": cancel_event,
            "task": current_task,
            "sender_id": message.sender_id,
            "status_msg": status_msg
        }

        cancel_btn = [[Button.inline("❌ Отменить", data=f"cancel_{task_id}")]]

        try:
            # 1. Загрузка файла (используем скоростной метод с прогрессом для всех файлов)
            # Внутри fast_download_file заложена логика автоматического перехода 
            # на стандартный метод (fallback), если скоростной дает ошибку.
            progress = ThrottledProgress(
                status_msg,
                action_title="Скачивание",
                buttons=cancel_btn,
                cancel_event=cancel_event
            )
            await status_msg.edit(f"📥 **Скачивание...**\nРазмер: {format_bytes(file_size)}", buttons=cancel_btn)

            downloaded_file = await fast_download_file(
                client=client,
                message=message,
                out_path=download_path,
                file_size=file_size,
                progress_callback=progress.callback,
                cancel_event=cancel_event
            )

            if cancel_event.is_set():
                raise asyncio.CancelledError()

            if not download_path.exists():
                await status_msg.edit("❌ Ошибка при скачивании файла.", buttons=None)
                return

            download_path = Path(downloaded_file)

            # Check User Mode
            user_mode = user_modes.get(message.sender_id, "transcribe")

            if user_mode == "uniqueize":
                # Добавили проверку расширений для .mov, .heic
                is_video = message.video or message.video_note or (message.document and (
                    (message.document.mime_type or "").startswith("video/") or 
                    (message.document.mime_type or "").startswith("image/heic") or
                    ext in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".heic"}
                ))
                if not is_video:
                    await status_msg.edit("⚠️ Файл не распознан как видео. Уникализация поддерживает только .mp4, .mov, .mkv и др.", buttons=None)
                    return

                print(f"[Debug] Starting uniquify for {download_path}", flush=True)
                uniq_progress = ThrottledUniquifyProgress(
                    status_msg,
                    action_title="Уникализация видео",
                    buttons=cancel_btn,
                    cancel_event=cancel_event
                )
                is_flip = user_flip.get(message.sender_id, False)
                flip_txt = " + зеркальное отражение" if is_flip else ""
                await status_msg.edit(
                    f"✨ **Уникализация видео{flip_txt}...**\n"
                    "Применение микро-зума, перцептивного шума, виньетки, микро-срезки I-frame и спуфинга метаданных...",
                    buttons=cancel_btn
                )
                
                # Замеряем время начала
                start_time = time.time()
                success, err_msg, meta_info = await async_uniquify_video(
                    download_path,
                    unique_video_path,
                    progress_callback=uniq_progress.callback,
                    cancel_event=cancel_event,
                    flip_horizontal=is_flip
                )
                duration = time.time() - start_time
                print(f"[Debug] Uniquify finished in {duration:.2f}s. Success: {success}", flush=True)

                if not success:
                    await status_msg.edit(f"❌ {err_msg}", buttons=None)
                    return

                orig_size = format_bytes(download_path.stat().st_size)
                new_size = format_bytes(unique_video_path.stat().st_size)
                device_str = meta_info.get("device", "Apple iPhone")
                encoder_str = meta_info.get("encoder", "FFmpeg")
                trim_sec = meta_info.get("trim", 0.0)
                trim_str = f"{trim_sec}с (новый I-frame)" if trim_sec > 0 else "Без срезки"
                flip_str = "ВКЛЮЧЕНО ✅" if meta_info.get("flip") else "ВЫКЛЮЧЕНО"

                await status_msg.edit(
                    f"🎬 **Видео успешно уникализировано!**\n\n"
                    f"📐 **Качество:** Студийное (CRF 18 visually lossless)\n"
                    f"⚡ **Движок:** `{encoder_str}`\n"
                    f"📱 **Профиль устройства:** `{device_str}`\n"
                    f"✂️ **Срезка начала:** `{trim_str}`\n"
                    f"🔍 **Зум / Скорость:** `{meta_info.get('zoom', '+2%')}` / `{meta_info.get('speed', '+1%')}`\n"
                    f"🪞 **Зеркало:** `{flip_str}`\n"
                    f"📦 **Размер:** `{new_size}` (исходный: `{orig_size}`)\n\n"
                    f"🚀 Видео готово к публикации в **Instagram Reels, TikTok и Shorts**!",
                    buttons=None
                )

                await client.send_file(
                    event.chat_id,
                    file=str(unique_video_path),
                    caption=f"✨ Уникализированное видео ({base_name})",
                    supports_streaming=True
                )
                return

            # 2. Extract Audio (cancel button removed since download is complete)
            await status_msg.edit("🎵 **Извлечение звуковой дорожки...**\nОптимизация под Whisper (16 kHz mono WAV)", buttons=None)
            extract_success, err_msg = await async_extract_audio(download_path, audio_wav_path)

            if not extract_success:
                await status_msg.edit(f"❌ {err_msg}", buttons=None)
                return

            # 3. Transcribe
            device_icon = "🚀 GPU" if transcriber.device == "cuda" else "💻 CPU"
            await status_msg.edit(
                f"🎙️ **Распознавание речи...**\n"
                f"Модель: `{config.WHISPER_MODEL}` | Ускоритель: `{device_icon}`"
            )

            result = await transcriber.transcribe(audio_wav_path)

            text = result["text"]
            timestamped_text = result["timestamped_text"]
            srt_content = result["srt"]
            lang_code = result["language"]
            lang_name = LANGUAGE_NAMES.get(lang_code, lang_code.upper())
            prob = int(result["language_probability"] * 100)
            duration_str = format_duration(result["audio_duration"])
            proc_time = result["processing_time"]

            if not text:
                await status_msg.edit("⚠️ В файле не обнаружена понятная речь.")
                return

            # 4. Save results to temporary files
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"--- Транскрибация: {base_name} ---\n")
                f.write(f"Длительность: {duration_str}\n")
                f.write(f"Язык: {lang_name} ({prob}%)\n\n")
                f.write("=== Текст с таймкодами ===\n")
                f.write(timestamped_text + "\n\n")
                f.write("=== Обычный текст ===\n")
                f.write(text + "\n")

            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(srt_content)

            # Generate formatted DOCX report
            create_docx_report(
                base_name=base_name,
                duration_str=duration_str,
                lang_name=lang_name,
                prob=prob,
                proc_time=proc_time,
                text=text,
                timestamped_text=timestamped_text,
                output_path=docx_path
            )

            # 5. Format message
            header = (
                f"🎬 **Распознавание завершено!**\n\n"
                f"⏱️ **Длительность:** `{duration_str}`\n"
                f"🌐 **Язык:** `{lang_name}` ({prob}%)\n"
                f"⚡ **Время обработки:** `{proc_time:.1f} сек`\n\n"
            )

            if len(text) <= 3200:
                message_body = f"{header}📝 **Текст:**\n{text}"
            else:
                snippet = text[:1500]
                message_body = (
                    f"{header}"
                    f"📝 **Текст (фрагмент):**\n{snippet}...\n\n"
                    f"📎 _Полный текст, субтитры и документ Word прикреплены ниже._"
                )

            try:
                await status_msg.edit(message_body)
            except Exception:
                # Fallback to plain text if speech transcription contains broken markdown characters
                try:
                    plain_header = (
                        f"🎬 Распознавание завершено!\n\n"
                        f"⏱️ Длительность: {duration_str}\n"
                        f"🌐 Язык: {lang_name} ({prob}%)\n"
                        f"⚡ Время обработки: {proc_time:.1f} сек\n\n"
                    )
                    snippet = text if len(text) <= 3200 else text[:1500] + "..."
                    await status_msg.edit(f"{plain_header}📝 Текст:\n{snippet}", parse_mode=None)
                except Exception:
                    pass

            # 6. Send files
            await client.send_file(
                event.chat_id,
                file=str(txt_path),
                caption=f"📄 Текст транскрибации ({base_name})"
            )
            await client.send_file(
                event.chat_id,
                file=str(srt_path),
                caption=f"🎬 Субтитры SRT ({base_name})"
            )
            if docx_path.exists():
                await client.send_file(
                    event.chat_id,
                    file=str(docx_path),
                    caption=f"📝 Документ Word ({base_name})"
                )

        except asyncio.CancelledError:
            try:
                await status_msg.edit("🚫 **Скачивание отменено пользователем.**", buttons=None)
            except Exception:
                pass
        except Exception as e:
            print(f"[Handler Error]: {e}")
            import traceback
            traceback.print_exc()
            try:
                await status_msg.edit(f"❌ Произошла ошибка при обработке: `{str(e)}`", buttons=None)
            except Exception:
                pass
        finally:
            active_tasks.pop(task_id, None)
            safe_remove(download_path)
            safe_remove(audio_wav_path)
            safe_remove(txt_path)
            safe_remove(srt_path)
            safe_remove(docx_path)
            safe_remove(unique_video_path)
    finally:
        task_queue.release()


def main():
    print("🚀 Подключение к Telegram (режим 2 ГБ / Telethon)...", flush=True)
    client.start(bot_token=config.BOT_TOKEN)
    me = client.loop.run_until_complete(client.get_me())

    # Регистрация меню команд в интерфейсе Telegram
    try:
        commands = [
            types.BotCommand(command="start", description="🚀 Главное меню / Перезапуск"),
            types.BotCommand(command="post", description="✍️ Оформить красивый ТГ-пост"),
            types.BotCommand(command="flip", description="🪞 Переключить зеркальное отражение"),
            types.BotCommand(command="help", description="📖 Инструкция и возможности"),
            types.BotCommand(command="settings", description="⚙️ Настройки и статус"),
        ]
        client.loop.run_until_complete(client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="",
            commands=commands
        )))
        print("✅ Меню команд Telegram (/start, /post, /help) успешно установлено!", flush=True)
    except Exception as e:
        print(f"⚠️ Не удалось зарегистрировать меню команд: {e}", flush=True)

    # Фоновые процессы автономности: очистка старых файлов и выгрузка памяти при простое
    client.loop.create_task(periodic_temp_cleanup(config.TEMP_DIR, interval_seconds=1800, max_age_seconds=3600))
    client.loop.create_task(transcriber.monitor_idle(idle_timeout=600, check_interval=60))

    print(f"✅ Бот @{me.username} успешно запущен и ожидает файлы до 2 ГБ в Telegram!", flush=True)
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
