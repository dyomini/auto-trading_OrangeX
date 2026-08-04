@echo off
rem This file intentionally contains only English text.
rem cmd.exe has a known bug where "chcp 65001" does not reliably apply to the
rem rest of a batch file, causing Korean text inside .bat files to break the
rem parser. All Korean user-facing messages live in setup_wizard.py instead,
rem since Python renders Unicode correctly (verified during development).

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this computer.
    echo Please install Python 3.11 or newer from https://www.python.org/downloads/
    echo IMPORTANT: check "Add python.exe to PATH" during installation.
    echo Then run this file again.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating a private environment for this program...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create the environment. See the error above.
        pause
        exit /b 1
    )
)

echo Installing required packages, this can take a few minutes...
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo Installation failed. Please check your internet connection and try again.
    pause
    exit /b 1
)

.venv\Scripts\python.exe setup_wizard.py

pause
