@echo off
cd /d "%~dp0"
chcp 65001 > nul
title Telegram Bot - Video to Text

set "PYTHON_EXE="

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=python"
    )
)

if "%PYTHON_EXE%"=="" (
    echo [ERROR] Python not found. Please install Python or create .venv
    pause
    exit /b 1
)

"%PYTHON_EXE%" -u bot.py

if errorlevel 1 (
    echo.
    echo [ERROR] Bot exited with error code.
    pause
)
