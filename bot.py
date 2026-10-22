import sys
import config

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main():
    if not config.BOT_TOKEN:
        print("\n" + "=" * 62)
        print("⚠️  ОШИБКА: Не указан BOT_TOKEN в файле .env!")
        print("=" * 62)
        print("Получите токен бота у @BotFather в Telegram и вставьте его в .env")
        print("=" * 62 + "\n")
        sys.exit(1)

    # Проверка и автоустановка FFmpeg при старте
    import audio_utils
    audio_utils.clean_temp_dir(config.TEMP_DIR)
    ffmpeg_bin = audio_utils.get_ffmpeg_executable(auto_install=True)
    if audio_utils.is_ffmpeg_functional(ffmpeg_bin):
        print(f"🎬 FFmpeg готов: {ffmpeg_bin}")
    else:
        print("⚠️  ВНИМАНИЕ: FFmpeg не найден и автоустановка не удалась!")

    has_telethon_credentials = bool(config.API_ID and config.API_HASH)

    if has_telethon_credentials:
        print("\n" + "=" * 62)
        print("🚀 ЗАПУСК БОТА В РЕЖИМЕ TELETHON (файлы до 2 ГБ)")
        if config.ALLOWED_USERS:
            print(f"🔒 Белый список: {', '.join(map(str, config.ALLOWED_USERS))}")
        print("=" * 62)
        import bot_telethon
        bot_telethon.main()
    else:
        print("\n" + "=" * 62)
        print("ℹ️  ЗАПУСК В СТАНДАРТНОМ РЕЖИМЕ (файлы до 20 МБ)")
        print("=" * 62)
        print("💡 Для поддержки больших файлов (до 2 ГБ):")
        print("   1. Получите API_ID и API_HASH на https://my.telegram.org")
        print("   2. Добавьте их в файл .env")
        print("   3. Перезапустите бота")
        print("=" * 62 + "\n")
        import bot_aiogram
        bot_aiogram.main()


if __name__ == "__main__":
    main()
