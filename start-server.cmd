@echo off
setlocal
cd /d "%~dp0"
if exist "runtime\pythonw.exe" (
    start "" "runtime\pythonw.exe" "desktop-launcher.py"
    exit /b 0
)
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "from importlib.metadata import version; assert version('qrcode') == '8.2'; assert version('zeroconf') == '0.151.5'" >nul 2>&1
if errorlevel 1 (
    echo Preparing Синхро...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet -e server
    if errorlevel 1 goto failed
)
cd server
"..\.venv\Scripts\python.exe" -m potbridge --open %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Синхро could not start. See the message above.
pause
exit /b 1
