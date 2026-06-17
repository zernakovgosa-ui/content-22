@echo off
rem ============================================================
rem  CLIPPER - standalone clip factory (port 8002)
rem  Hard-kills the old server + cleans __pycache__ first, so
rem  stale code never keeps running (same lesson as START.bat).
rem ============================================================
setlocal
cd /d "%~dp0"

echo [1/4] Killing anything on port 8002...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8002" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

echo [2/4] Cleaning __pycache__...
for /d /r "%~dp0clipper" %%d in (__pycache__) do if exist "%%d" rd /s /q "%%d" >nul 2>&1
for /d /r "%~dp0packages" %%d in (__pycache__) do if exist "%%d" rd /s /q "%%d" >nul 2>&1

set "PYEXE=%~dp0trezzy-video-worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    echo [X] venv not found. Run START.bat once to create it.
    pause
    exit /b 1
)

echo [3/4] Starting Clipper on http://127.0.0.1:8002 ...
start "CLIPPER" "%PYEXE%" -m uvicorn clipper.server:app --host 127.0.0.1 --port 8002

echo [4/4] Opening dashboard...
timeout /t 3 /nobreak >nul
start http://127.0.0.1:8002

echo.
echo Clipper is running. Close the CLIPPER window to stop it.
endlocal
