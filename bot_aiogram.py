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

import socket
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession

import config
from audio_utils import format_duration, format_bytes, async_extract_audio, safe_remove, sanitize_filename, create_docx_report
from transcriber import transcriber
from video_uniquifier import async_uniquify_video

class IPv4AiohttpSession(AiohttpSession):
    """Ensures IPv4 connection to avoid Windows IPv6 semaphore timeout issues."""
    async def create_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                ssl=self._ssl,
                family=socket.AF_INET,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                json_serialize=self.json_dumps,
            )
        return self._session

dp = Dispatcher()

class TaskQueueTracker:
    """Manages concurrent processing tasks and notifies users of their queue position."""
    def __init__(self, max_concurrent: int):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.waiting = []
        self.lock = asyncio.Lock()

    async def acquire(self, status_msg: Message):
        async with self.lock:
            self.waiting.append(status_msg)
            pos = len(self.waiting)

        if pos > 1:
            try:
                await status_msg.edit_text(f"⏳ **В очереди:** позиция `{pos}`\nОбработка начнётся автоматически, как только освободится сервер...", parse_mode="Markdown")
            except Exception:
                pass

        await self.semaphore.acquire()

        async with self.lock:
            if status_msg in self.waiting:
                self.waiting.remove(status_msg)
            for idx, msg in enumerate(self.waiting, start=1):
                try:
                    await msg.edit_text(f"⏳ **В очереди:** позиция `{idx}`\nОбработка начнётся автоматически...", parse_mode="Markdown")
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


def get_file_info(message: Message):
    """Returns (file_id, file_size, base_name, extension)"""
    timestamp = int(time.time())
    if message.video:
        return message.video.file_id, message.video.file_size, f"video_{timestamp}", ".mp4"
    if message.video_note:
        return message.video_note.file_id, message.video_note.file_size, f"circle_{timestamp}", ".mp4"
    if message.voice:
        return message.voice.file_id, message.voice.file_size, f"voice_{timestamp}", ".ogg"
    if message.audio:
        ext = Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"
        raw_stem = Path(message.audio.file_name or f"audio_{timestamp}").stem
        return message.audio.file_id, message.audio.file_size, sanitize_filename(raw_stem), ext
    if message.document:
        doc_name = message.document.file_name or f"file_{timestamp}"
        ext = Path(doc_name).suffix.lower()
        mime = (message.document.mime_type or "").lower()
        if ext in MEDIA_EXTENSIONS or mime.startswith("video/") or mime.startswith("audio/"):
            raw_stem = Path(doc_name).stem
            return message.document.file_id, message.document.file_size, sanitize_filename(raw_stem), ext or ".mp4"
    return None, None, None, None


user_modes = {}  # user_id -> 'transcribe' | 'uniqueize'


def get_mode_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎙️ Видео в Текст"),
                KeyboardButton(text="✨ Уникализировать видео")
            ]
        ],
        resize_keyboard=True
    )


def check_access(user_id: int) -> bool:
    if not config.ALLOWED_USERS:
        return True
    return user_id in config.ALLOWED_USERS


@dp.message(Command("start"))
async def start_handler(message: Message):
    if not check_access(message.from_user.id):
        await message.answer("⛔ **Доступ ограничен.**\nВы не авторизованы для использования этого бота.", parse_mode="Markdown")
        return

    user_modes[message.from_user.id] = "transcribe"
    welcome_text = (
        "👋 **Привет! Я бот для работы с видео и аудио.**\n\n"
        "⚡ **Выберите нужный режим кнопками ниже:**\n\n"
        "1️⃣ **🎙️ Видео в Текст:**\n"
        "• Распознавание речи из видео, кружочков, аудио и голосовых\n"
        "• Создание субтитров `.srt`, текста с таймкодами и документа Word `.docx`\n\n"
        "2️⃣ **✨ Уникализировать видео:**\n"
        "• Подготовка видео для **Instagram Reels, TikTok и Shorts**\n"
        "• Уникализация **без потери качества** (микро-зум, перцептивный шум, цветовой сдвиг, смена аудиохэша, очистка метаданных)\n"
        "• Позволяет обойти фильтры повторного контента и теневой бан"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=get_mode_keyboard())


@dp.message(F.text == "🎙️ Видео в Текст")
async def mode_transcribe_handler(message: Message):
    if not check_access(message.from_user.id):
        return
    user_modes[message.from_user.id] = "transcribe"
    await message.answer(
        "🎙️ **Включен режим: Видео в Текст**\n\n"
        "Отправьте мне видео, кружочек, аудиофайл или голосовое сообщение — я переведу речь в текст, создам файл субтитров .srt и документ Word!",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard()
    )


@dp.message(F.text == "✨ Уникализировать видео")
async def mode_uniqueize_handler(message: Message):
    if not check_access(message.from_user.id):
        return
    user_modes[message.from_user.id] = "uniqueize"
    await message.answer(
        "✨ **Включен режим: Уникализация видео**\n\n"
        "Отправьте мне видео или кружочек — я уникализирую его без потери качества (микро-зум, микро-сдвиг цветов, перцептивный шум, замена аудиохэша и очистка метаданных) для безопасного размещения в **Instagram Reels, TikTok и Shorts**!",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard()
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    if not check_access(message.from_user.id):
        await message.answer("⛔ **Доступ ограничен.**", parse_mode="Markdown")
        return

    help_text = (
        "📖 **Инструкция по использованию:**\n\n"
        "1. Отправьте видео, кружочек, аудиофайл или голосовое сообщение.\n"
        "2. Бот извлечёт аудиодорожку и распознает речь с помощью нейросети Whisper.\n"
        "3. После завершения бот пришлёт:\n"
        "   — Текст сообщения прямо в чат\n"
        "   — Текстовый файл `.txt` с полным текстом и таймкодами\n"
        "   — Файл субтитров `.srt`\n\n"
        f"⚙️ **Текущая конфигурация:**\n"
        f"• Модель Whisper: `{config.WHISPER_MODEL}`\n"
        f"• Устройство обработки: `{transcriber.device.upper()}`\n"
        f"• Точность (compute_type): `{transcriber.compute_type}`\n"
    )
    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("settings"))
async def settings_handler(message: Message):
    if not check_access(message.from_user.id):
        await message.answer("⛔ **Доступ ограничен.**", parse_mode="Markdown")
        return

    settings_text = (
        "⚙️ **Настройки бота:**\n\n"
        f"• **Whisper модель:** `{config.WHISPER_MODEL}`\n"
        f"• **Устройство:** `{transcriber.device.upper()}`\n"
        f"• **Compute Type:** `{transcriber.compute_type}`\n"
        f"• **Язык распознавания:** `{config.WHISPER_LANGUAGE or 'Автоопределение'}`\n"
        f"• **Поиск гипотез (beam_size):** `{config.BEAM_SIZE}`\n"
        f"• **Одновременных задач:** `{config.MAX_CONCURRENT_TASKS}`\n\n"
        "💡 Для изменения параметров отредактируйте файл `.env` и перезапустите бота."
    )
    await message.answer(settings_text, parse_mode="Markdown")


@dp.message(F.video | F.video_note | F.audio | F.voice | F.document)
async def media_handler(message: Message, bot: Bot):
    if not check_access(message.from_user.id):
        await message.reply("⛔ **Доступ ограничен.**\nВы не авторизованы для использования этого бота.", parse_mode="Markdown")
        return

    file_id, file_size, base_name, ext = get_file_info(message)
    if not file_id:
        return

    # Check 20MB limit for standard Bot API
    if file_size and file_size > 20 * 1024 * 1024:
        warning_msg = (
            f"⚠️ **Файл слишком большой ({format_bytes(file_size)})!**\n\n"
            "Стандартный протокол Telegram Bot API ограничивает скачивание файлов до 20 МБ.\n\n"
            "🚀 **Как включить поддержку файлов до 2 ГБ:**\n"
            "1. Получите `API_ID` и `API_HASH` на сайте https://my.telegram.org\n"
            "2. Вставьте их в файл `.env`\n"
            "3. Перезапустите бота\n"
            "После этого бот сможет обрабатывать видео размером до 2 ГБ!"
        )
        await message.reply(warning_msg, parse_mode="Markdown")
        return

    status_msg = await message.reply("⏳ Добавлено в очередь на обработку...")

    await task_queue.acquire(status_msg)
    try:
        timestamp_id = int(time.time() * 1000)
        unique_prefix = f"{timestamp_id}_{message.from_user.id}"

        download_path = config.TEMP_DIR / f"{unique_prefix}_input{ext}"
        audio_wav_path = config.TEMP_DIR / f"{unique_prefix}_audio.wav"
        txt_path = config.TEMP_DIR / f"{base_name}.txt"
        srt_path = config.TEMP_DIR / f"{base_name}.srt"
        docx_path = config.TEMP_DIR / f"{base_name}.docx"
        unique_video_path = config.TEMP_DIR / f"{unique_prefix}_unique.mp4"

        try:
            # 1. Download
            await status_msg.edit_text(f"📥 **Скачивание файла...**\nРазмер: {format_bytes(file_size or 0)}", parse_mode="Markdown")
            tg_file = await bot.get_file(file_id)
            await bot.download_file(tg_file.file_path, destination=download_path)

            if not download_path.exists() or download_path.stat().st_size == 0:
                await status_msg.edit_text("❌ Ошибка при скачивании файла из Telegram.")
                return

            # Check User Mode
            user_mode = user_modes.get(message.from_user.id, "transcribe")

            if user_mode == "uniqueize":
                is_video = message.video or message.video_note or (message.document and ((message.document.mime_type or "").startswith("video/") or ext in {".mp4", ".mov", ".mkv", ".avi", ".webm"}))
                if not is_video:
                    await status_msg.edit_text("⚠️ В режиме уникализации поддерживаются только видеофайлы и кружочки. Для аудио переключитесь кнопкой на режим 🎙️ Видео в Текст.")
                    return

                await status_msg.edit_text(
                    "✨ **Уникализация видео...**\n"
                    "Применение микро-зума, перцептивного шума, цветокоррекции и очистки метаданных...",
                    parse_mode="Markdown"
                )
                success, err_msg = await async_uniquify_video(download_path, unique_video_path)
                if not success:
                    await status_msg.edit_text(f"❌ {err_msg}")
                    return

                orig_size = format_bytes(download_path.stat().st_size)
                new_size = format_bytes(unique_video_path.stat().st_size)

                await status_msg.edit_text(
                    f"🎬 **Видео успешно уникализировано!**\n\n"
                    f"📐 **Качество:** Исходное (CRF 18 visually lossless)\n"
                    f"📦 **Размер:** `{new_size}` (исходный: `{orig_size}`)\n"
                    f"🛡️ **Изменения:** Микро-зум 1.5%, перцептивный шум, сдвиг аудиохэша, очистка метаданных.\n"
                    f"🚀 Видео готово к публикации в **Instagram Reels, TikTok и Shorts**!",
                    parse_mode="Markdown"
                )

                await message.reply_video(
                    video=FSInputFile(str(unique_video_path)),
                    caption=f"✨ Уникализированное видео ({base_name})"
                )
                return

            # 2. Extract Audio
            await status_msg.edit_text("🎵 **Извлечение звуковой дорожки...**\nОптимизация под Whisper (16 kHz mono WAV)", parse_mode="Markdown")
            extract_success, err_msg = await async_extract_audio(download_path, audio_wav_path)

            if not extract_success:
                await status_msg.edit_text(f"❌ {err_msg}")
                return

            # 3. Transcribe
            device_icon = "🚀 GPU" if transcriber.device == "cuda" else "💻 CPU"
            await status_msg.edit_text(
                f"🎙️ **Распознавание речи...**\n"
                f"Модель: `{config.WHISPER_MODEL}` | Ускоритель: `{device_icon}`",
                parse_mode="Markdown"
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
                await status_msg.edit_text("⚠️ В файле не обнаружена понятная речь.")
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

            # 5. Send results
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
                    f"{header}📝 **Текст (фрагмент):**\n{snippet}...\n\n"
                    f"📎 _Полный текст, субтитры и документ Word прикреплены ниже._"
                )

            try:
                await status_msg.edit_text(message_body, parse_mode="Markdown")
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
                    await status_msg.edit_text(f"{plain_header}📝 Текст:\n{snippet}", parse_mode=None)
                except Exception:
                    pass

            # 6. Send files
            await message.reply_document(
                document=FSInputFile(str(txt_path)),
                caption=f"📄 Текст транскрибации ({base_name})"
            )
            await message.reply_document(
                document=FSInputFile(str(srt_path)),
                caption=f"🎬 Субтитры SRT ({base_name})"
            )
            if docx_path.exists():
                await message.reply_document(
                    document=FSInputFile(str(docx_path)),
                    caption=f"📝 Документ Word ({base_name})"
                )

        except Exception as e:
            print(f"[Handler Error]: {e}")
            import traceback
            traceback.print_exc()
            try:
                await status_msg.edit_text(f"❌ Произошла ошибка: `{str(e)}`", parse_mode="Markdown")
            except Exception:
                pass
        finally:
            safe_remove(download_path)
            safe_remove(audio_wav_path)
            safe_remove(txt_path)
            safe_remove(srt_path)
            safe_remove(docx_path)
            safe_remove(unique_video_path)
    finally:
        task_queue.release()


async def run_aiogram_bot():
    session = IPv4AiohttpSession()
    bot = Bot(token=config.BOT_TOKEN, session=session)
    print("🚀 Подготовка Whisper модели...")
    transcriber.ensure_model_loaded()
    me = await bot.get_me()
    print(f"✅ Бот @{me.username} успешно запущен в стандартном режиме (aiogram) и ожидает файлы в Telegram!")
    await dp.start_polling(bot)


def main():
    asyncio.run(run_aiogram_bot())


if __name__ == "__main__":
    main()
