@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Madrasa Premium Offline
if not exist .venv\Scripts\python.exe (
  echo Creating Python virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 (
    echo ERROR: Python 3.11 or newer is required. Install it and try again.
    pause
    exit /b 1
  )
)
rem --- Install packages only if missing. Works fully offline if a "wheels" folder exists.
.venv\Scripts\python.exe -c "import fastapi,uvicorn,jinja2,multipart" >nul 2>&1
if errorlevel 1 (
  if exist wheels (
    echo Installing packages from local "wheels" folder - no internet needed...
    .venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements.txt
  ) else (
    echo Installing packages - internet needed this first time only...
    .venv\Scripts\python.exe -m pip install -r requirements.txt
  )
  if errorlevel 1 (
    echo.
    echo ERROR: Package installation failed. See README_UR.txt - "Offline install" section.
    pause
    exit /b 1
  )
)
echo.
echo Starting Madrasa Premium Offline...  Close this window to stop.
start "" http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
