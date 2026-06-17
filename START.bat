@echo off
REM ============================================================
REM   TREZZY Content Factory - ONE-CLICK LAUNCHER (v2)
REM   Kills any old server first, then starts fresh.
REM ============================================================
setlocal enableextensions
cd /d "%~dp0"
title TREZZY Content Factory

echo.
echo ===============================================
echo    TREZZY Content Factory
echo ===============================================
echo.

REM --- 0. Python check ------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python ne nayden. Ustanovi Python 3.11+ s python.org
  echo     ^(galka "Add Python to PATH" pri ustanovke^).
  pause
  exit /b 1
)

set "WORKER=%~dp0trezzy-video-worker"
set "VENVPY=%WORKER%\.venv\Scripts\python.exe"

REM --- 1. HARD STOP any previous server -------------------------
echo [*] Ostanavlivayu staryy server (esli zapushchen) ...
REM kill processes holding ports 8000/8001
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>nul
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8001 " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>nul
REM also kill any stray python from our venv
taskkill /F /IM python.exe >nul 2>nul
timeout /t 2 /nobreak >nul
echo [+] Staryy server ostanovlen.

REM --- 2. venv on first run -------------------------------------
if not exist "%VENVPY%" (
  echo [*] Pervyy zapusk: sozdayu okruzhenie ...
  pushd "%WORKER%"
  python -m venv .venv
  if errorlevel 1 ( echo [X] venv error & popd & pause & exit /b 1 )
  echo [*] Stavlyu zavisimosti (2-5 min) ...
  "%VENVPY%" -m pip install --upgrade pip
  "%VENVPY%" -m pip install -r requirements.txt
  popd
  echo [+] Okruzhenie gotovo.
) else (
  echo [+] Okruzhenie est - propuskayu.
)

REM --- 3. clear python cache so fresh code always loads ---------
echo [*] Chishchu kesh ...
for /d /r "%~dp0" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul

REM --- 4. start worker + api -----------------------------------
echo [*] Zapuskayu Worker na :8000 ...
start "TREZZY Worker" cmd /k ""%VENVPY%" "%WORKER%\main.py""

echo [*] Zapuskayu API + Dashboard na :8001 ...
start "TREZZY API + Dashboard" cmd /k "cd /d "%~dp0" && set PYTHONPATH=%~dp0&& "%VENVPY%" -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8001"

echo.
echo [*] Zhdu 6 sek i otkryvayu brauzer ...
timeout /t 6 /nobreak >nul
start "" "http://127.0.0.1:8001/"

echo.
echo ===============================================
echo  GOTOVO!  Dashboard: http://127.0.0.1:8001/
echo  Ostanovit: zakroy dva chernyh okna ili zapusti STOP.bat
echo ===============================================
timeout /t 4 /nobreak >nul
exit /b 0
