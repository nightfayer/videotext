"""
post_formatter.py - High-quality Telegram post generator and rich HTML formatter.
"""

import re
import html
import logging
import asyncio
import aiohttp
from typing import Tuple
import config

logger = logging.getLogger(__name__)

POST_SYSTEM_PROMPT = """Ты — топовый Telegram-автор и редактор уютного лайфстайл-канала.
Твоя задача — взять сырой текст, расшифровку кружочка/видео, черновик или мысли от автора и превратить их в идеальный, живой, атмосферный и легкий пост для Telegram в фирменном стиле личного блога.

ГЛАВНЫЕ ПРАВИЛА СТИЛЯ И ВАЙБА:
1. НИКАКОЙ ВОДЫ И СЕРВИСНЫХ ФРАЗ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать вступления вроде: «Конечно!», «Вот ваш пост:», «Привет! Держи оформление...».
   - Выдавай ТОЛЬКО готовый текст поста, готовый к мгновенной публикации!

2. ИНТОНАЦИЯ И ТОН (TONE OF VOICE):
   - Разговорный, искренний, ламповый, подружеский тон — как будто автор делится со своими подписчиками в кружочке или голосовом сообщении.
   - Никаких занудных маркетинговых клише («уникальные преимущества», «в этой статье», «5 причин почему»).
   - Живой сленг и разговорные обороты: «любимки», «зашел», «тюбик», «кайф», «просто жуть», «смекаете», «пока так)», «вывод: пора...».
   - Можно начинать со строчной буквы («в каком прекрасно-красивом месте...»), ироничного третьего лица («она снова ходила на тренировку 🏐») или короткого капс-хука («КТО НЕ ЗНАЛ ✌️»).

3. ВОЗДУХ И РИТМИКА ТЕКСТА:
   - Максимум «воздуха»! Между каждым блоком ОБЯЗАТЕЛЬНО пустая строка.
   - 1 короткое предложение на абзац (максимум 2 коротких). Никаких сплошных кирпичей текста! Читатель должен легко скользить по экрану телефона за 10-15 секунд.

4. ТИПОГРАФИКА И ОФОРМЛЕНИЕ:
   - <i>Курсив</i>: ОБЯЗАТЕЛЬНО используй 1-2 раза в посте для передачи эмоций, внутренних мыслей, ощущений и тонких наблюдений (например: <i>ехать было долго, но оно и правда того стоило ✨</i> или <i>хочется еще 🥹</i>).
   - <blockquote>Цитата</blockquote> (через `> текст`): выдели 1 ключевой инсайт, главное ощущение тела или кульминационный вывод в блок цитаты.
   - <code>Инлайн-код</code>: выделяй локации, бренды, артикулы, названия мест или моделей (например: 📍 <code>Переделкино</code> или 🛍 <code>Ozon</code>).
   - Эмодзи: живые, милые и эмоциональные (🥹, 💔, ✨, 🤤, 🫣, 🥰, 🥲, ✌️, 💖, 🫧, 🤕, 🩹, 🏠, 🐱, 🍊, 😂). Ставь их в конце ключевых фраз как естественную реакцию.

ОБРАЗЦЫ ИДЕАЛЬНЫХ ПОСТОВ (СТРОГО ОРИЕНТИРУЙСЯ НА НИХ):

Пример 1 (Радость / покупка / обновление):
коллеги, поздравьте меня!

<i>теперь по сути, звук должен быть лучше 🥹</i>

> но вы узнаете об этом, если смотрите мой инст/тикток/ютуб
> а если нет, то не узнаете

смекаете, к чему я?

P.S. Подарок от Максима 💖
Я рада сильно 🥹

Пример 2 (Подборка / личный мини-рейтинг):
КТО НЕ ЗНАЛ ✌️

я любитель бланшей безалкогольных 🫣 🤤

<i>мои любимки на данный момент:</i>

1 с выраженным кориандром 🐱
2 легкий бланш, особо что-то выделить не могу, все в совокупности, вкус однородный - цельный
3 выражен цитрус 🍊, но тем не менее приятный и легкий

пока так)

Пример 3 (Тренировка / спорт / день из жизни):
она снова ходила на тренировку 🏐 🌟

<i>два часа пролетают просто мгновенно, хочется еще 🥹</i>

> после игры пошла ещё гулять
> ноги сейчас забитые - просто жуть, но при этом одновременно кайф 🥰

сожгла вторую коленку 🤕
вывод: пора заказывать наколенники 🩹

<i>хотелось бы, конечно, ходить не раз в неделю, а 2-3 раза</i>

Пример 4 (Впечатления от атмосферного места / поездка):
в каком прекрасно-красивом месте я была сегодня 💔

<i>ехать было долго, но оно и правда того стоило ✨ 🫧 😍</i>

📍 <code>Переделкино</code>

мы зашли в книжный и какой же он был красивый
> как будто я реально зашла в чей-то дом 🏠 приятное чувство

Пример 5 (Честный отзыв на средство / вещь):
я в восторге с тюбика этого 🥹

состав хорош!
я, в основном, пользуюсь флюидами, а не просто кремами

<i>так как текстура «просто крема» для меня плотная 🥲</i>

> поэтому вот этот лосьон мне зашел

он кстати, ОКАЗЫВАЕТСЯ, больше для нормальной и сухой кожи, а также обезвоженной 😂

<i>но у меня осенью/зимой бывает такая проблема с обезвоженностью - поэтому сейчас он тоже самое то 🥰</i>
"""

ALLOWED_TAGS = {
    "b", "strong",
    "i", "em",
    "u", "ins",
    "s", "strike", "del",
    "tg-spoiler",
    "a",
    "code",
    "pre",
    "blockquote"
}

def sanitize_telegram_html(text: str) -> str:
    """
    Cleans, normalizes, and balances HTML according to Telegram Bot API requirements.
    """
    # 1. Replace common HTML block/break tags
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*hr\s*/?>", "\n---\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*p\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*li\s*>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/li\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/?(?:ul|ol|div|span)[^>]*>", "", text, flags=re.IGNORECASE)

    # 2. Extract and validate allowed HTML tags, escaping any stray < or > characters
    valid_tags = []
    def tag_extractor(match):
        tag_content = match.group(1).strip()
        if tag_content.startswith("/"):
            tag_name = tag_content[1:].split()[0].lower() if tag_content[1:].split() else ""
            if tag_name in ALLOWED_TAGS:
                valid_tags.append(f"</{tag_name}>")
                return f"___VALIDTAG_{len(valid_tags)-1}___"
            return ""

        parts = tag_content.split(None, 1)
        if not parts:
            return ""
        tag_name = parts[0].lower()
        attr = parts[1] if len(parts) > 1 else ""

        if tag_name not in ALLOWED_TAGS:
            return ""

        if tag_name == "blockquote":
            if "expandable" in attr.lower():
                valid_tags.append("<blockquote expandable>")
            else:
                valid_tags.append("<blockquote>")
            return f"___VALIDTAG_{len(valid_tags)-1}___"

        if tag_name == "code":
            if "class=" in attr:
                m_class = re.search(r'class=["\']?(language-[\w+-]+)["\']?', attr)
                if m_class:
                    valid_tags.append(f'<code class="{m_class.group(1)}">')
                    return f"___VALIDTAG_{len(valid_tags)-1}___"
            valid_tags.append("<code>")
            return f"___VALIDTAG_{len(valid_tags)-1}___"

        if tag_name == "a":
            m_href = re.search(r'href=["\'](https?://[^"\']+)["\']', attr)
            if m_href:
                valid_tags.append(f'<a href="{m_href.group(1)}">')
                return f"___VALIDTAG_{len(valid_tags)-1}___"
            return ""

        valid_tags.append(f"<{tag_name}>")
        return f"___VALIDTAG_{len(valid_tags)-1}___"

    text = re.sub(r"<(/?[a-zA-Z][a-zA-Z0-9\-_]*(?:\s+[^<>]*)?)>", tag_extractor, text)

    # Escape any remaining raw < or > so Telegram HTML parser never fails on "<3" or "->"
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    # Restore valid tags
    for idx, tag_str in enumerate(valid_tags):
        text = text.replace(f"___VALIDTAG_{idx}___", tag_str)

    # 3. Balance allowed tags
    tag_regex = re.compile(r"<\s*(/)?\s*([a-zA-Z0-9\-_]+)(?:\s+[^>]*)?>")
    stack = []
    balanced_chunks = []
    last_idx = 0

    for match in tag_regex.finditer(text):
        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()

        if tag_name not in ALLOWED_TAGS:
            continue

        if is_closing:
            if stack and stack[-1] == tag_name:
                stack.pop()
                balanced_chunks.append(text[last_idx:match.end()])
                last_idx = match.end()
            elif tag_name in stack:
                balanced_chunks.append(text[last_idx:match.start()])
                while stack and stack[-1] != tag_name:
                    unclosed = stack.pop()
                    balanced_chunks.append(f"</{unclosed}>")
                if stack and stack[-1] == tag_name:
                    stack.pop()
                balanced_chunks.append(f"</{tag_name}>")
                last_idx = match.end()
            else:
                balanced_chunks.append(text[last_idx:match.start()])
                last_idx = match.end()
        else:
            stack.append(tag_name)
            balanced_chunks.append(text[last_idx:match.end()])
            last_idx = match.end()

    balanced_chunks.append(text[last_idx:])
    balanced_text = "".join(balanced_chunks)

    while stack:
        unclosed = stack.pop()
        balanced_text += f"</{unclosed}>"

    balanced_text = re.sub(r"\n{3,}", "\n\n", balanced_text)
    return balanced_text.strip()


def md_to_telegram_html(text: str) -> str:
    """
    Converts Markdown and mixed HTML to clean, robust Telegram HTML.
    """
    if not text:
        return ""

    # Protect code blocks
    code_blocks = []
    def save_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        code_blocks.append((lang.strip(), code))
        return f"___CODEBLOCK_{len(code_blocks)-1}___"

    text = re.sub(r"```(\w*)\n?(.*?)```", save_code_block, text, flags=re.DOTALL)

    inline_codes = []
    def save_inline_code(m):
        inline_codes.append(m.group(1))
        return f"___INLINECODE_{len(inline_codes)-1}___"

    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # Convert blockquotes: > line or &gt; line
    lines = text.split("\n")
    processed_lines = []
    in_quote = False
    quote_acc = []

    for line in lines:
        stripped = line.strip()
        is_gt = stripped.startswith(">") and not stripped.startswith(">>")
        is_escaped_gt = stripped.startswith("&gt;")
        if is_gt or is_escaped_gt:
            in_quote = True
            if is_escaped_gt:
                idx = line.find("&gt;") + 4
            else:
                idx = line.find(">") + 1
            content = line[idx:].strip()
            quote_acc.append(content)
        else:
            if in_quote:
                quote_text = "\n".join(quote_acc)
                processed_lines.append(f"<blockquote>{quote_text}</blockquote>")
                quote_acc = []
                in_quote = False
            processed_lines.append(line)
    if in_quote:
        quote_text = "\n".join(quote_acc)
        processed_lines.append(f"<blockquote>{quote_text}</blockquote>")

    text = "\n".join(processed_lines)

    # Convert Headers: # Header -> <b>Header</b>
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Convert lists: - item: desc -> • <b>item:</b> desc
    text = re.sub(r"^[ \t]*[\*\-][ \t]+([^\:\n]{1,40})\:([ \t]+.+)$", r"• <b>\1:</b>\2", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*[\*\-][ \t]+(.+)$", r"• \1", text, flags=re.MULTILINE)

    # Bold italic & formatting
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*\*_(.+?)_\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"_\*\*(.+?)\*\*_", r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)([^\*\n]+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)([^_\n]+?)(?<!_)_(?!_)", r"<i>\1</i>", text)

    # Restore inline codes
    for i, code in enumerate(inline_codes):
        escaped = html.escape(code)
        text = text.replace(f"___INLINECODE_{i}___", f"<code>{escaped}</code>")

    # Restore code blocks
    for i, (lang, code) in enumerate(code_blocks):
        escaped = html.escape(code)
        if lang:
            tag = f'<pre><code class="language-{lang}">{escaped}</code></pre>'
        else:
            tag = f'<pre><code>{escaped}</code></pre>'
        text = text.replace(f"___CODEBLOCK_{i}___", tag)

    return sanitize_telegram_html(text)


async def generate_telegram_post(
    raw_text: str,
    max_tokens: int = 4096,
    temperature: float = 0.7
) -> Tuple[bool, str, str]:
    """
    Takes raw draft/text and generates a beautifully formatted Telegram post.
    Returns: (success: bool, html_post: str, error_message: str)
    """
    if not config.NVIDIA_API_KEY:
        return False, "", "⚠️ В файле `.env` не настроен `NVIDIA_API_KEY`."

    clean_text = raw_text.strip()
    if not clean_text:
        return False, "", "⚠️ Текст для оформления пуст."

    if len(clean_text) > 15000:
        clean_text = clean_text[:15000] + "\n\n[...Текст сокращен для соблюдения лимита контекста...]"

    url = f"{config.NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "model": config.NVIDIA_MODEL,
        "messages": [
            {"role": "system", "content": POST_SYSTEM_PROMPT},
            {"role": "user", "content": f"Вот текст для оформления в пост:\n\n{clean_text}"}
        ],
        "chat_template_kwargs": {
            "enable_thinking": True
        },
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.95
    }

    timeout = aiohttp.ClientTimeout(total=90)
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 451:
                        err_body = await resp.text()
                        logger.error(f"[NVIDIA API Geo-Block 451]: {err_body}")
                        return False, "", (
                            "⛔ Ошибка 451 (Геоблокировка NVIDIA):\n"
                            "IP-адрес сервера заблокирован NVIDIA по региональному признаку (РФ)."
                        )

                    if resp.status in (429, 502, 503, 504) and attempt < max_retries:
                        logger.warning(f"[NVIDIA API Busy {resp.status}], attempt {attempt}/{max_retries}. Retrying in 2s...")
                        await asyncio.sleep(2.0 * attempt)
                        continue

                    if resp.status != 200:
                        err_body = await resp.text()
                        logger.error(f"[NVIDIA API Error {resp.status}]: {err_body}")
                        return False, "", f"Ошибка API ({resp.status}): {err_body[:150]}"

                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return False, "", "Нейросеть вернула пустой ответ."

                    msg_obj = choices[0].get("message") or {}
                    raw_post = (msg_obj.get("content") or "").strip()
                    # Удаляем возможные блоки размышлений модели (<think>...</think>, <thought>...</thought>), включая незакрытые
                    raw_post = re.sub(r"<(?:think|thought)>.*?</(?:think|thought)>", "", raw_post, flags=re.DOTALL).strip()
                    raw_post = re.sub(r"^<(?:think|thought)>.*", "", raw_post, flags=re.DOTALL).strip()
                    if not raw_post:
                        return False, "", "Получен пустой текст ответа от модели."

                    # Convert to formatted Telegram HTML
                    formatted_html = md_to_telegram_html(raw_post)
                    return True, formatted_html, ""

        except (aiohttp.ClientConnectorError, TimeoutError) as e:
            if attempt < max_retries:
                await asyncio.sleep(2.0 * attempt)
                continue
            return False, "", f"Ошибка подключения к серверам нейросети: {e}"
        except Exception as e:
            logger.error(f"Unexpected error in generate_telegram_post: {e}", exc_info=True)
            return False, "", f"Ошибка генерации поста: {str(e)}"

    return False, "", "Серверы нейросети временно перегружены. Попробуйте еще раз через минуту."
