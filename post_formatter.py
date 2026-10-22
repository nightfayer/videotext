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

POST_SYSTEM_PROMPT = """Ты — топовый Telegram-редактор и автор популярных каналов.
Твоя задача — взять сырой текст, черновик, мысли, обзор или расшифровку от пользователя и превратить их в идеальный, красивый, атмосферный и легкий для чтения пост для Telegram.

ПРАВИЛА И СТИЛЬ ОФОРМЛЕНИЯ:
1. СРАЗУ К СУТИ БЕЗ ВОДЫ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать любые вступления и сервисные фразы: «Конечно!», «Вот ваш пост:», «Привет! С радостью помогу...».
   - Выдавай ТОЛЬКО сам готовый текст поста, готовый к публикации!

2. ЭСТЕТИКА И ФОРМАТИРОВАНИЕ ШРИФТОВ:
   - <b>Жирный шрифт</b>: используй для цепляющего заголовка, главных фактов, акцентов и ключевых выводов.
   - <i>Курсив</i>: активно используй для передачи эмоций, личных мыслей, впечатлений, чувств и тонких моментов (например: <i>ехать было долго, но оно и правда того стоило✨</i>).
   - <b><i>Жирный курсив</i></b>: для самых эмоциональных и кульминационных фраз поста.
   - <code>Инлайн-код</code>: выделяй локации, бренды, артикулы, названия мест или моделей (например: 📍<code>Переделкино</code> или 🛍<code>Ozon</code>).
   - <blockquote>Цитата</blockquote>: выдели ключевой инсайт, важный вывод или фразу автора в блок цитаты (> текст цитаты).
   - Разделитель «——»: используй перед финальной мыслью или подписью, когда уместно.

3. ВОЗДУХ И РИТМ:
   - Очень много «воздуха» между абзацами! Никаких монолитных кирпичей текста.
   - 1-2 коротких предложения на абзац, обязательно разделенные пустой строкой. Читатель должен легко скользить по тексту на экране смартфона.

4. ЖИВЫЕ ЭМОДЗИ И ВАЙБ:
   - Эмодзи должны быть живыми и органичными (✨, 💔, 🥹, 🤌🏻, 🫂, 🏠, ☕️, 🤍, 🔥), ставь их в конце фраз или как эмоциональную реакцию, без перебора.
   - Тон: искренний, душевный, современный — будто автор делится со своими подписчиками без формализма, штампов и сухости.
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
    # 1. Replace line breaks and paragraphs
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*hr\s*/?>", "\n---\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*p\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*li\s*>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/li\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/?(?:ul|ol|div|span)[^>]*>", "", text, flags=re.IGNORECASE)

    # 2. Filter allowed tags
    def tag_filter(match):
        tag_content = match.group(1).strip()
        if tag_content.startswith("/"):
            tag_name = tag_content[1:].split()[0].lower()
            if tag_name in ALLOWED_TAGS:
                return f"</{tag_name}>"
            return ""

        parts = tag_content.split(None, 1)
        tag_name = parts[0].lower()
        attr = parts[1] if len(parts) > 1 else ""

        if tag_name not in ALLOWED_TAGS:
            return ""

        if tag_name == "blockquote":
            if "expandable" in attr.lower():
                return "<blockquote expandable>"
            return "<blockquote>"

        if tag_name == "code":
            if "class=" in attr:
                m_class = re.search(r'class=["\']?(language-[\w+-]+)["\']?', attr)
                if m_class:
                    return f'<code class="{m_class.group(1)}">'
            return "<code>"

        if tag_name == "a":
            m_href = re.search(r'href=["\'](https?://[^"\']+)["\']', attr)
            if m_href:
                return f'<a href="{m_href.group(1)}">'
            return ""

        return f"<{tag_name}>"

    text = re.sub(r"<([^>]+)>", tag_filter, text)

    # 3. Balance tags
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
                while stack and stack[-1] != tag_name:
                    unclosed = stack.pop()
                    balanced_chunks.append(f"</{unclosed}>")
                if stack and stack[-1] == tag_name:
                    stack.pop()
                balanced_chunks.append(text[last_idx:match.end()])
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
    Converts Markdown to clean, robust Telegram HTML.
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

    # Convert blockquotes: > line
    lines = text.split("\n")
    processed_lines = []
    in_quote = False
    quote_acc = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("&gt; ") or stripped.startswith("> "):
            in_quote = True
            content = line[line.find(">") + 1:].strip()
            quote_acc.append(content)
        else:
            if in_quote:
                quote_text = html.escape("\n".join(quote_acc))
                processed_lines.append(f"<blockquote>{quote_text}</blockquote>")
                quote_acc = []
                in_quote = False
            processed_lines.append(line)
    if in_quote:
        quote_text = html.escape("\n".join(quote_acc))
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
    max_tokens: int = 1500,
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

                    raw_post = choices[0].get("message", {}).get("content", "").strip()
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
