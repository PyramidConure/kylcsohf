@echo off
setlocal
cd /d "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" goto dependencies
if not "%~1"=="" goto custom
py -3 -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if not errorlevel 1 goto launcher
python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if errorlevel 1 goto missing
python -m venv .venv || goto failed
goto dependencies

:custom
"%~1" -c "import sys; sys.exit(sys.version_info < (3, 12))" || goto missing
"%~1" -m venv .venv || goto failed
goto dependencies

:launcher
py -3 -m venv .venv || goto failed

:dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 12))" || goto missing
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt || goto failed
echo Installation complete. Run start.cmd.
exit /b 0

:missing
echo Install Python 3.12 or later or pass the full python.exe path to install.cmd.
goto failed

:failed
echo Installation failed.
pause
exit /b 1
