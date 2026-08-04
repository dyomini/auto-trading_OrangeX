@echo off
rem English-only wrapper (see install.bat for why) - all Korean text lives in
rem launcher.py.

if not exist .venv (
    echo Please run install.bat first.
    pause
    exit /b 1
)
if not exist .env (
    echo Please run install.bat first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe launcher.py

pause
