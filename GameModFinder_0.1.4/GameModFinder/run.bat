@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Python version check...
py -3 --version
if errorlevel 1 goto no_python

if not exist ".venv\Scripts\python.exe" (
    echo [2/3] Creating virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 goto setup_error
)

echo [2/3] Installing required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto setup_error

echo [3/3] Starting Game Mod Finder...
".venv\Scripts\python.exe" main.py
if errorlevel 1 goto app_error

exit /b 0

:no_python
echo.
echo Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo During installation, enable the Python Launcher and Add Python to PATH.
pause
exit /b 1

:setup_error
echo.
echo Setup failed. Check your Python installation and internet connection.
pause
exit /b 1

:app_error
echo.
echo Game Mod Finder stopped because of an error. See the message above.
pause
exit /b 1
