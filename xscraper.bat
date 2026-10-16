@echo off
setlocal

REM Run from the project directory regardless of the caller's current directory.
cd /d "%~dp0"
if errorlevel 1 (
    echo Error: cannot switch to "%~dp0".
    pause
    exit /b 1
)

REM Prefer the project virtual environment; fall back to Python on PATH.
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" "x_collector_server.py"
if errorlevel 1 (
    echo.
    echo The collector stopped with an error.
)
pause
