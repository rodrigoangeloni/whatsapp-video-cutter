@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Cortador y Compresor WhatsApp v1.0.0

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: No se pudo crear .venv. Verifica que Python 3.10+ este instalado.
        pause
        exit /b 1
    )
    .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
)

echo Iniciando Cortador y Compresor para WhatsApp en .venv...
".venv\Scripts\python.exe" "%~dp0whatsapp_cutter.py"
pause
