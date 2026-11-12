import asyncio
import re
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
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    FSInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand
)
from aiogram.client.session.aiohttp import AiohttpSession

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
from transcriber import transcriber
from video_uniquifier import async_uniquify_video
from post_formatter import generate_telegram_post, sanitize_telegram_html

class IPv4AiohttpSession(AiohttpSession):
    """Ensures IPv4 connection to avoid Windows/Linux IPv6 semaphore timeout issues."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connector_init["family"] = socket.AF_INET

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


recent_transcripts = {}   # msg_id -> full_text (cache up to 50 transcripts for 1-click post generation)
recent_post_sources = {}  # msg_id -> raw_text (cache up to 50 post drafts for regen/shorten)
admin_broadcast_wait = set()     # admin_id -> waiting for broadcast message
admin_broadcast_drafts = {}      # admin_id -> (chat_id, message_id) for preview/send


def build_uniqueize_message(user_id: int):
    is_flip = config.get_user_flip(user_id)
    flip_status = "ВКЛЮЧЕНО ✅" if is_flip else "ВЫКЛЮЧЕНО ❌"
    flip_btn_label = "🪞 Зеркало: ВКЛ ✅ (выключить)" if is_flip else "🪞 Зеркало: ВЫКЛ ❌ (включить)"
    text = (
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
        "💡 _Нажмите кнопку ниже или отправьте /flip для переключения зеркала._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=flip_btn_label, callback_data="toggle_flip")]
    ])
    return text, kb


def get_post_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Другой вариант", callback_data="post_regen"),
        InlineKeyboardButton(text="✂️ Короче", callback_data="post_shorter"),
    ]])


def _cache_post_source(msg_id: int, raw_text: str):
    if len(recent_post_sources) >= 50:
        recent_post_sources.pop(next(iter(recent_post_sources)), None)
    recent_post_sources[msg_id] = raw_text


class ThrottledUniquifyProgress:
    def __init__(self, message: Message, action_title="Уникализация видео"):
        self.message = message
        self.action_title = action_title
        self.last_update_time = 0.0
        self.last_percent = -1

    async def callback(self, current_sec: float, total_sec: float, speed_str: str = ""):
        if not total_sec or total_sec <= 0:
            return

        percent = min(100, int((current_sec / total_sec) * 100))
        now = time.time()

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
                await self.message.edit_text(text, parse_mode="Markdown")
            except Exception:
                pass


def get_mode_keyboard(user_id: int = None) -> ReplyKeyboardMarkup:
    kb = [
        [
            KeyboardButton(text="🎙️ Видео в Текст"),
            KeyboardButton(text="✨ Уникализировать видео")
        ],
        [
            KeyboardButton(text="✍️ Написать ТГ-пост"),
            KeyboardButton(text="🚀 Меню / Старт")
        ]
    ]
    if user_id and config.is_admin(user_id):
        kb.append([
            KeyboardButton(text="👑 Управление доступом")
        ])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def check_access(user_id: int) -> bool:
    return config.is_user_allowed(user_id)


def build_admin_panel(admin_id: int):
    users = config.get_allowed_users_list()
    lines = []
    for uid in users:
        if config.is_admin(uid):
            label = "👑 Главный админ" if uid == config.DEFAULT_ADMIN_ID else "👑 Админ"
            if uid == admin_id:
                label += " (Вы)"
            lines.append(f"• `{uid}` — **{label}**")
        else:
            lines.append(f"• `{uid}`")

    users_text = "\n".join(lines) if lines else "_(Список пуст)_"

    text = (
        "👑 **Панель управления доступом**\n\n"
        f"🔒 **Пользователи в белом списке ({len(users)}):**\n"
        f"{users_text}\n\n"
        "⚡ **Быстрые команды:**\n"
        "➕ Добавить: `/adduser <ID>`\n"
        "➖ Удалить: `/deluser <ID>`\n\n"
        "💡 _Также можно переслать сообщение пользователя с командой /adduser или нажать кнопки ниже:_"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить пользователя", callback_data="adm_add_help"),
            InlineKeyboardButton(text="➖ Удалить пользователя", callback_data="adm_del_menu")
        ],
        [
            InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="adm_broadcast")
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить список", callback_data="adm_refresh")
        ]
    ])
    return text, kb


@dp.message(Command("start"))
@dp.message(F.text == "🚀 Меню / Старт")
async def start_handler(message: Message):
    if not check_access(message.from_user.id):
        await message.answer(
            f"⛔ **Доступ ограничен.**\n\n"
            f"Вы не авторизованы для использования этого бота.\n\n"
            f"🆔 Ваш Telegram ID: `{message.from_user.id}`\n"
            f"💡 _Отправьте этот ID администратору для получения доступа._",
            parse_mode="Markdown"
        )
        return

    config.set_user_mode(message.from_user.id, "transcribe")
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
        "• Цепляющий заголовок, четкая структура с акцентами, цитаты и хэштеги"
    )
    if config.is_admin(message.from_user.id):
        welcome_text += "\n\n👑 _Вы авторизованы как Администратор бота (доступна панель управления)._"

    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=get_mode_keyboard(message.from_user.id))


@dp.message(F.text == "🎙️ Видео в Текст")
async def mode_transcribe_handler(message: Message):
    if not check_access(message.from_user.id):
        return
    config.set_user_mode(message.from_user.id, "transcribe")
    await message.answer(
        "🎙️ **Включен режим: Видео в Текст**\n\n"
        "Отправьте мне видео, кружочек, аудиофайл или голосовое сообщение — я переведу речь в текст, создам файл субтитров .srt и документ Word!",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard(message.from_user.id)
    )


@dp.message(F.text == "✨ Уникализировать видео")
async def mode_uniqueize_handler(message: Message):
    if not check_access(message.from_user.id):
        return
    config.set_user_mode(message.from_user.id, "uniqueize")
    text, kb = build_uniqueize_message(message.from_user.id)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "toggle_flip")
async def flip_inline_callback_handler(callback: CallbackQuery):
    if not check_access(callback.from_user.id):
        await callback.answer("⛔ Доступ ограничен.", show_alert=True)
        return
    new_val = not config.get_user_flip(callback.from_user.id)
    config.set_user_flip(callback.from_user.id, new_val)
    text, kb = build_uniqueize_message(callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass
    await callback.answer("🪞 Зеркало ВКЛЮЧЕНО ✅" if new_val else "🪞 Зеркало ВЫКЛЮЧЕНО ❌")


@dp.message(Command("flip"))
async def flip_toggle_handler(message: Message):
    if not check_access(message.from_user.id):
        return
    new_val = not config.get_user_flip(message.from_user.id)
    config.set_user_flip(message.from_user.id, new_val)
    state_str = "ВКЛЮЧЕНО ✅" if new_val else "ВЫКЛЮЧЕНО ❌"
    tip = (
        "Видео будет отражено по горизонтали (зеркальный эффект для максимального обхода алгоритмов соцсетей)."
        if new_val
        else "Видео будет обработано в стандартной ориентации (без зеркального отражения)."
    )
    await message.answer(
        f"🪞 **Зеркальное отражение видео:** `{state_str}`\n\n{tip}",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard(message.from_user.id)
    )


@dp.message(F.text == "✍️ Написать ТГ-пост")
async def mode_post_handler(message: Message):
    if not check_access(message.from_user.id):
        return
    config.set_user_mode(message.from_user.id, "post")
    await message.answer(
        "✍️ **Включен режим: Оформление ТГ-постов**\n\n"
        "Пришлите мне **текстовый черновик, мысли, голосовое сообщение, кружочек или видео** — я распознаю речь и сразу оформлю всё в готовый, живой пост для Telegram!",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard(message.from_user.id)
    )


@dp.message(Command("admin"))
@dp.message(Command("users"))
@dp.message(F.text == "👑 Управление доступом")
async def admin_panel_handler(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ **Доступ запрещен.** Эта команда доступна только администраторам бота.", parse_mode="Markdown")
        return
    text, kb = build_admin_panel(message.from_user.id)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.message(Command("adduser", "add_user", "allow"))
async def add_user_handler(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ **Доступ запрещен.** Эта команда доступна только администраторам бота.", parse_mode="Markdown")
        return

    text_parts = (message.text or "").split(maxsplit=1)
    raw_args = text_parts[1].strip() if len(text_parts) > 1 else ""

    target_ids = []

    if raw_args:
        tokens = re.split(r'[\s,]+', raw_args)
        for t in tokens:
            if t.isdigit():
                target_ids.append(int(t))
    elif message.reply_to_message:
        reply = message.reply_to_message
        if reply.forward_from:
            target_ids.append(reply.forward_from.id)
        elif reply.from_user:
            target_ids.append(reply.from_user.id)

    if not target_ids:
        await message.answer(
            "➕ **Как добавить пользователя:**\n\n"
            "• Отправьте: `/adduser <Telegram ID>`\n"
            "  _Пример:_ `/adduser 123456789`\n"
            "• Или добавьте несколько ID сразу:\n"
            "  `/adduser 123456789 987654321`\n"
            "• Либо ответьте командой `/adduser` на пересланное сообщение пользователя.",
            parse_mode="Markdown",
            reply_markup=get_mode_keyboard(message.from_user.id)
        )
        return

    results = []
    for uid in target_ids:
        ok, msg = config.add_allowed_user(uid)
        results.append(msg)

    await message.answer("\n".join(results), parse_mode="Markdown", reply_markup=get_mode_keyboard(message.from_user.id))


@dp.message(Command("deluser", "del_user", "remove_user", "deny"))
async def del_user_handler(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ **Доступ запрещен.** Эта команда доступна только администраторам бота.", parse_mode="Markdown")
        return

    text_parts = (message.text or "").split(maxsplit=1)
    raw_args = text_parts[1].strip() if len(text_parts) > 1 else ""

    target_ids = []

    if raw_args:
        tokens = re.split(r'[\s,]+', raw_args)
        for t in tokens:
            if t.isdigit():
                target_ids.append(int(t))
    elif message.reply_to_message:
        reply = message.reply_to_message
        if reply.forward_from:
            target_ids.append(reply.forward_from.id)
        elif reply.from_user:
            target_ids.append(reply.from_user.id)

    if not target_ids:
        await message.answer(
            "➖ **Как удалить пользователя:**\n\n"
            "• Отправьте: `/deluser <Telegram ID>`\n"
            "  _Пример:_ `/deluser 123456789`\n"
            "• Или нажмите кнопку **👑 Управление доступом** и выберите пользователя для удаления.",
            parse_mode="Markdown",
            reply_markup=get_mode_keyboard(message.from_user.id)
        )
        return

    results = []
    for uid in target_ids:
        ok, msg = config.remove_allowed_user(uid)
        results.append(msg)

    await message.answer("\n".join(results), parse_mode="Markdown", reply_markup=get_mode_keyboard(message.from_user.id))


@dp.callback_query(F.data.startswith("adm_"))
async def admin_callback_handler(callback: CallbackQuery):
    if not config.is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен.", show_alert=True)
        return

    data = callback.data

    if data == "adm_refresh":
        text, kb = build_admin_panel(callback.from_user.id)
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            await callback.answer("🔄 Список пользователей обновлен!")
        except Exception:
            await callback.answer("Список уже актуален.")

    elif data == "adm_add_help":
        await callback.answer(
            "Отправьте команду:\n/adduser <Telegram ID>\n\nПример:\n/adduser 123456789",
            show_alert=True
        )

    elif data == "adm_del_menu":
        non_admins = [u for u in config.get_allowed_users_list() if not config.is_admin(u)]
        if not non_admins:
            await callback.answer("В белом списке нет пользователей, доступных для удаления.", show_alert=True)
            return

        del_buttons = []
        row = []
        for uid in non_admins:
            row.append(InlineKeyboardButton(text=f"❌ {uid}", callback_data=f"adm_rm_{uid}"))
            if len(row) == 2:
                del_buttons.append(row)
                row = []
        if row:
            del_buttons.append(row)

        del_buttons.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="adm_refresh")])
        await callback.message.edit_text(
            "🗑️ **Выберите пользователя для удаления из белого списка:**\n\n"
            "💡 _Нажмите на кнопку с ID нужного пользователя, чтобы мгновенно закрыть ему доступ к боту:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=del_buttons)
        )

    elif data.startswith("adm_rm_"):
        uid_str = data.removeprefix("adm_rm_")
        if uid_str.isdigit():
            uid = int(uid_str)
            ok, msg = config.remove_allowed_user(uid)
            await callback.answer(f"Пользователь {uid} удален из белого списка ❌", show_alert=False)
            text, kb = build_admin_panel(callback.from_user.id)
            try:
                await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            except Exception:
                pass

    elif data == "adm_broadcast":
        admin_broadcast_wait.add(callback.from_user.id)
        admin_broadcast_drafts.pop(callback.from_user.id, None)
        await callback.message.edit_text(
            "📢 **Создание уведомления (рассылки)**\n\n"
            "Пришлите сюда сообщение, которое получат все пользователи из белого списка:\n"
            "• **Текст** (с любым форматированием Telegram)\n"
            "• **Картинку** с подписью или без\n"
            "• **GIF / анимацию** (или короткое видео) с подписью или без\n\n"
            "💡 _Перед отправкой бот покажет вам предпросмотр и попросит подтверждение._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data="adm_bcast_cancel")]
            ])
        )
        await callback.answer()

    elif data == "adm_bcast_cancel":
        admin_broadcast_wait.discard(callback.from_user.id)
        admin_broadcast_drafts.pop(callback.from_user.id, None)
        text, kb = build_admin_panel(callback.from_user.id)
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            pass
        await callback.answer("Рассылка отменена.")

    elif data == "adm_bcast_send":
        draft = admin_broadcast_drafts.pop(callback.from_user.id, None)
        admin_broadcast_wait.discard(callback.from_user.id)
        if not draft:
            await callback.answer("⚠️ Черновик рассылки устарел или не найден.", show_alert=True)
            return

        from_chat_id, msg_id = draft
        recipients = [u for u in config.get_allowed_users_list() if u != callback.from_user.id]
        if not recipients:
            await callback.message.edit_text(
                "ℹ️ **В белом списке пока нет других пользователей, кроме вас.**\n\n"
                "Добавьте пользователей через `/adduser <ID>`, чтобы делать рассылку.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="adm_refresh")]
                ])
            )
            await callback.answer()
            return

        await callback.answer("🚀 Запускаю рассылку...")
        try:
            await callback.message.edit_text(
                f"📢 **Идёт рассылка ({len(recipients)} польз.)...**",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        sent_ok = 0
        failed = 0
        for uid in recipients:
            try:
                await callback.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=from_chat_id,
                    message_id=msg_id
                )
                sent_ok += 1
            except Exception as e:
                print(f"[Broadcast Warning] Не удалось отправить {uid}: {e}", flush=True)
                failed += 1
            await asyncio.sleep(0.05)

        await callback.message.answer(
            f"📢 **Рассылка завершена!**\n\n"
            f"✅ Доставлено: `{sent_ok}`\n"
            f"⚠️ Не доставлено (не запускали бота / блок): `{failed}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="adm_refresh")]
            ])
        )


@dp.message(
    F.from_user.id.func(lambda uid: uid in admin_broadcast_wait),
    ~(F.text & (F.text.startswith("/") | F.text.in_({
        "🎙️ Видео в Текст", "✨ Уникализировать видео",
        "✍️ Написать ТГ-пост", "🚀 Меню / Старт", "👑 Управление доступом"
    })))
)
async def admin_broadcast_capture_handler(message: Message):
    if not config.is_admin(message.from_user.id):
        admin_broadcast_wait.discard(message.from_user.id)
        return

    admin_broadcast_wait.discard(message.from_user.id)
    admin_broadcast_drafts[message.from_user.id] = (message.chat.id, message.message_id)
    recipients_count = len([u for u in config.get_allowed_users_list() if u != message.from_user.id])

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить ({recipients_count} польз.)", callback_data="adm_bcast_send")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="adm_bcast_cancel")]
    ])

    await message.answer("👀 **Предпросмотр уведомления** (так его увидят пользователи):", parse_mode="Markdown")
    await message.bot.copy_message(
        chat_id=message.chat.id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_markup=confirm_kb
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    if not check_access(message.from_user.id):
        await message.answer("⛔ **Доступ ограничен.**", parse_mode="Markdown")
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
        f"• Режим: `Bot API (до 20 МБ)`\n"
        f"• Whisper: `{config.WHISPER_MODEL}` | Ускоритель: `{transcriber.device.upper()}`\n"
        f"• AI-редактор: `{config.NVIDIA_MODEL}`\n"
    )
    await message.answer(help_text, parse_mode="Markdown")


@dp.callback_query(F.data == "makepost")
async def makepost_callback_handler(callback: CallbackQuery):
    if not check_access(callback.from_user.id):
        await callback.answer("⛔ Доступ ограничен.", show_alert=True)
        return

    raw_text = recent_transcripts.get(callback.message.message_id, "")
    if not raw_text and callback.message and callback.message.text:
        m = re.search(r"📝\s*(?:\*\*Текст(?:\s*\(фрагмент\))?:\*\*|Текст:)\s*\n(.+)", callback.message.text, flags=re.DOTALL)
        raw_text = m.group(1).strip() if m else callback.message.text.strip()

    if not raw_text:
        await callback.answer("⚠️ Текст для поста не найден.", show_alert=True)
        return

    await callback.answer("✍️ Генерирую пост из расшифровки...")
    await process_and_send_post(callback.message, raw_text)


@dp.message(Command("post"))
async def post_cmd_handler(message: Message):
    if not check_access(message.from_user.id):
        return

    raw_text = ""
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].strip():
        raw_text = args[1].strip()
    elif message.reply_to_message:
        reply = message.reply_to_message
        if reply.message_id in recent_transcripts:
            raw_text = recent_transcripts[reply.message_id]
        elif reply.text or reply.caption:
            txt = reply.text or reply.caption
            m = re.search(r"📝\s*(?:\*\*Текст(?:\s*\(фрагмент\))?:\*\*|Текст:)\s*\n(.+)", txt, flags=re.DOTALL)
            raw_text = m.group(1).strip() if m else txt.strip()

    if not raw_text:
        await message.reply(
            "✍️ **Как оформить ТГ-пост:**\n\n"
            "• Либо переключитесь кнопкой в режим **✍️ Написать ТГ-пост** и пришлите текст сообщением.\n"
            "• Либо отправьте: `/post <ваш черновик>`\n"
            "• Либо ответьте командой `/post` на любое сообщение с текстом.",
            parse_mode="Markdown",
            reply_markup=get_mode_keyboard(message.from_user.id)
        )
        return

    await process_and_send_post(message, raw_text)


@dp.callback_query(F.data.in_({"post_regen", "post_shorter"}))
async def post_action_callback_handler(callback: CallbackQuery):
    if not check_access(callback.from_user.id):
        await callback.answer("⛔ Доступ ограничен.", show_alert=True)
        return

    action = callback.data
    raw_text = recent_post_sources.get(callback.message.message_id, "")
    if not raw_text and callback.message and callback.message.text:
        raw_text = callback.message.text.strip()

    if not raw_text:
        await callback.answer("⚠️ Исходный текст не найден. Отправьте его ещё раз.", show_alert=True)
        return

    if action == "post_regen":
        await callback.answer("🔄 Пишу другой вариант...")
        style_hint = "alt"
        status_label = "🔄 **Генерирую другой вариант поста...**"
    else:
        await callback.answer("✂️ Сокращаю пост...")
        style_hint = "shorter"
        status_label = "✂️ **Делаю пост короче и ёмче...**"

    try:
        await callback.message.edit_text(
            f"{status_label}\nМодель: `{config.NVIDIA_MODEL}` ⚡",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    success, html_post, err = await generate_telegram_post(raw_text, style_hint=style_hint)
    post_kb = get_post_action_keyboard()
    if not success:
        await callback.message.edit_text(f"❌ {err or 'Не удалось обновить пост.'}", reply_markup=post_kb)
        return

    if len(html_post) <= 4000:
        _cache_post_source(callback.message.message_id, raw_text)
        try:
            await callback.message.edit_text(html_post, parse_mode="HTML", reply_markup=post_kb)
        except Exception:
            await callback.message.edit_text(html_post, parse_mode=None, reply_markup=post_kb)
    else:
        part1 = sanitize_telegram_html(html_post[:3800])
        part2 = sanitize_telegram_html(html_post[3800:])
        try:
            await callback.message.edit_text(part1, parse_mode="HTML")
            sent_msg = await callback.message.reply(part2, parse_mode="HTML", reply_markup=post_kb)
        except Exception:
            await callback.message.edit_text(part1, parse_mode=None)
            sent_msg = await callback.message.reply(part2, parse_mode=None, reply_markup=post_kb)
        _cache_post_source(sent_msg.message_id, raw_text)


@dp.message(F.text & ~F.text.startswith('/'))
async def text_handler(message: Message):
    if not check_access(message.from_user.id):
        await message.answer(
            f"⛔ **Доступ ограничен.**\n\n"
            f"🆔 Ваш Telegram ID: `{message.from_user.id}`\n"
            f"💡 _Отправьте этот ID администратору для получения доступа._",
            parse_mode="Markdown"
        )
        return

    text = (message.text or "").strip()
    if not text:
        return

    if text in ["🎙️ Видео в Текст", "✨ Уникализировать видео", "✍️ Написать ТГ-пост", "🚀 Меню / Старт", "👑 Управление доступом"]:
        return

    current_mode = config.get_user_mode(message.from_user.id)
    if current_mode == "post":
        await process_and_send_post(message, text)
    elif current_mode == "uniqueize":
        await message.answer(
            "⚠️ **Включен режим: Уникализация видео.**\n\n"
            "Пожалуйста, пришлите видеофайл или кружочек.\n"
            "Чтобы красиво оформить текст в пост, переключитесь кнопкой на **✍️ Написать ТГ-пост**.",
            parse_mode="Markdown",
            reply_markup=get_mode_keyboard(message.from_user.id)
        )
    else:
        await message.answer(
            "🎙️ **Включен режим: Видео в Текст.**\n\n"
            "Пришлите видео, кружочек или аудиофайл для расшифровки речи.\n"
            "Чтобы красиво оформить текст в пост, переключитесь кнопкой на **✍️ Написать ТГ-пост**.",
            parse_mode="Markdown",
            reply_markup=get_mode_keyboard(message.from_user.id)
        )


async def process_and_send_post(message: Message, raw_text: str):
    progress_msg = await message.reply(
        "✍️ **Оформляю красивый пост для Telegram...**\n"
        f"Модель: `{config.NVIDIA_MODEL}` ⚡",
        parse_mode="Markdown"
    )
    success, html_post, err = await generate_telegram_post(raw_text)
    if not success:
        await progress_msg.edit_text(f"❌ {err or 'Не удалось оформить пост.'}")
        return

    post_kb = get_post_action_keyboard()
    if len(html_post) <= 4000:
        _cache_post_source(progress_msg.message_id, raw_text)
        try:
            await progress_msg.edit_text(html_post, parse_mode="HTML", reply_markup=post_kb)
        except Exception:
            try:
                await progress_msg.edit_text(html_post, parse_mode=None, reply_markup=post_kb)
            except Exception:
                pass
    else:
        part1 = sanitize_telegram_html(html_post[:3800])
        part2 = sanitize_telegram_html(html_post[3800:])
        try:
            await progress_msg.edit_text(part1, parse_mode="HTML")
            sent_msg = await message.reply(part2, parse_mode="HTML", reply_markup=post_kb)
        except Exception:
            await progress_msg.edit_text(part1, parse_mode=None)
            sent_msg = await message.reply(part2, parse_mode=None, reply_markup=post_kb)
        _cache_post_source(sent_msg.message_id, raw_text)


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
        await message.reply(
            f"⛔ **Доступ ограничен.**\n\n"
            f"🆔 Ваш Telegram ID: `{message.from_user.id}`\n"
            f"💡 _Отправьте этот ID администратору для получения доступа._",
            parse_mode="Markdown"
        )
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
            user_mode = config.get_user_mode(message.from_user.id)

            if user_mode == "uniqueize":
                # Добавили проверку расширений для .mov, .heic
                is_video = message.video or message.video_note or (message.document and (
                    (message.document.mime_type or "").startswith("video/") or 
                    (message.document.mime_type or "").startswith("image/heic") or
                    ext in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".heic"}
                ))
                if not is_video:
                    await status_msg.edit_text("⚠️ Файл не распознан как видео. Уникализация поддерживает только .mp4, .mov, .mkv и др.")
                    return

                print(f"[Debug] Starting uniquify for {download_path}", flush=True)
                uniq_progress = ThrottledUniquifyProgress(
                    status_msg,
                    action_title="Уникализация видео"
                )
                is_flip = config.get_user_flip(message.from_user.id)
                flip_txt = " + зеркальное отражение" if is_flip else ""
                await status_msg.edit_text(
                    f"✨ **Уникализация видео{flip_txt}...**\n"
                    "Применение микро-зума, перцептивного шума, виньетки, микро-срезки I-frame и спуфинга метаданных...",
                    parse_mode="Markdown"
                )
                
                # Замеряем время
                start_time = time.time()
                success, err_msg, meta_info = await async_uniquify_video(
                    download_path,
                    unique_video_path,
                    progress_callback=uniq_progress.callback,
                    flip_horizontal=is_flip
                )
                duration = time.time() - start_time
                print(f"[Debug] Uniquify finished in {duration:.2f}s. Success: {success}", flush=True)

                if not success:
                    await status_msg.edit_text(f"❌ {err_msg}")
                    return

                orig_size = format_bytes(download_path.stat().st_size)
                new_size = format_bytes(unique_video_path.stat().st_size)
                device_str = meta_info.get("device", "Apple iPhone")
                encoder_str = meta_info.get("encoder", "FFmpeg")
                trim_sec = meta_info.get("trim", 0.0)
                trim_str = f"{trim_sec}с (новый I-frame)" if trim_sec > 0 else "Без срезки"
                flip_str = "ВКЛЮЧЕНО ✅" if meta_info.get("flip") else "ВЫКЛЮЧЕНО"

                await status_msg.edit_text(
                    f"🎬 **Видео успешно уникализировано!**\n\n"
                    f"📐 **Качество:** Студийное (CRF 18 visually lossless)\n"
                    f"⚡ **Движок:** `{encoder_str}`\n"
                    f"📱 **Профиль устройства:** `{device_str}`\n"
                    f"✂️ **Срезка начала:** `{trim_str}`\n"
                    f"🔍 **Зум / Скорость:** `{meta_info.get('zoom', '+2%')}` / `{meta_info.get('speed', '+1%')}`\n"
                    f"🪞 **Зеркало:** `{flip_str}`\n"
                    f"📦 **Размер:** `{new_size}` (было: `{orig_size}`)\n\n"
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

            # Если включен режим «Написать ТГ-пост», сразу превращаем расшифровку голоса/кружочка в готовый пост!
            if user_mode == "post":
                await status_msg.edit_text(
                    "✍️ **Речь распознана! Оформляю пост для Telegram...**\n"
                    f"Модель: `{config.NVIDIA_MODEL}` ⚡",
                    parse_mode="Markdown"
                )
                success_p, html_post, err_p = await generate_telegram_post(text)
                if not success_p:
                    await status_msg.edit_text(
                        f"⚠️ **Не удалось сгенерировать пост:** {err_p}\n\n"
                        f"📝 **Расшифрованный текст:**\n{text[:3000]}"
                    )
                    return

                post_kb = get_post_action_keyboard()
                if len(html_post) <= 4000:
                    _cache_post_source(status_msg.message_id, text)
                    try:
                        await status_msg.edit_text(html_post, parse_mode="HTML", reply_markup=post_kb)
                    except Exception:
                        await status_msg.edit_text(html_post, parse_mode=None, reply_markup=post_kb)
                else:
                    p1 = sanitize_telegram_html(html_post[:3800])
                    p2 = sanitize_telegram_html(html_post[3800:])
                    try:
                        await status_msg.edit_text(p1, parse_mode="HTML")
                        sent_msg = await message.reply(p2, parse_mode="HTML", reply_markup=post_kb)
                    except Exception:
                        await status_msg.edit_text(p1, parse_mode=None)
                        sent_msg = await message.reply(p2, parse_mode=None, reply_markup=post_kb)
                    _cache_post_source(sent_msg.message_id, text)
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

            if len(recent_transcripts) >= 50:
                recent_transcripts.pop(next(iter(recent_transcripts)), None)
            recent_transcripts[status_msg.message_id] = text
            post_inline_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Оформить в ТГ-пост", callback_data="makepost")]
            ])

            try:
                await status_msg.edit_text(message_body, parse_mode="Markdown", reply_markup=post_inline_kb)
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
                    await status_msg.edit_text(f"{plain_header}📝 Текст:\n{snippet}", parse_mode=None, reply_markup=post_inline_kb)
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
    me = await bot.get_me()

    # Регистрация меню команд в интерфейсе Telegram
    try:
        commands = [
            BotCommand(command="start", description="🚀 Главное меню / Перезапуск"),
            BotCommand(command="post", description="✍️ Оформить красивый ТГ-пост"),
            BotCommand(command="flip", description="🪞 Переключить зеркальное отражение"),
            BotCommand(command="admin", description="👑 Управление доступом (Админ)"),
            BotCommand(command="help", description="📖 Инструкция и возможности"),
            BotCommand(command="settings", description="⚙️ Настройки и статус"),
        ]
        await bot.set_my_commands(commands)
        print("✅ Меню команд Telegram (/start, /post, /help) успешно установлено!")
    except Exception as e:
        print(f"⚠️ Не удалось зарегистрировать меню команд: {e}")

    # Фоновые процессы автономности: очистка старых файлов и выгрузка памяти при простое
    asyncio.create_task(periodic_temp_cleanup(config.TEMP_DIR, interval_seconds=1800, max_age_seconds=3600))
    asyncio.create_task(transcriber.monitor_idle(idle_timeout=600, check_interval=60))

    print(f"✅ Бот @{me.username} успешно запущен в стандартном режиме (aiogram) и ожидает файлы в Telegram!")
    await dp.start_polling(bot)


def main():
    asyncio.run(run_aiogram_bot())


if __name__ == "__main__":
    main()
