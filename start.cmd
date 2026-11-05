@echo off
setlocal
cd /d "%~dp0" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo Run install.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m creator_control %*
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
