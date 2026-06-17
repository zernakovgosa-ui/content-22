@echo off
REM ============================================================
REM   TREZZY - prostoy nadezhnyy zapusk dashboard (API na 8001)
REM   Server rabotaet pryamo v etom okne. Okno NE zakryvaetsya,
REM   oshibki vidny. Vorker (8000) ne nuzhen dlya dashboard/clip.
REM ============================================================
cd /d "%~dp0"
title TREZZY Dashboard
set "VENVPY=%~dp0trezzy-video-worker\.venv\Scripts\python.exe"
set "PYTHONPATH=%~dp0"
set "PYTHONUTF8=1"

echo ===============================================
echo    TREZZY Content Factory - RUN
echo ===============================================
echo.

if not exist "%VENVPY%" (
  echo [X] Okruzhenie venv ne naydeno:
  echo     "%VENVPY%"
  echo.
  echo     Zapusti START.bat odin raz - on sozdast venv ^(2-5 min^),
  echo     potom snova zapusti RUN.bat.
  echo.
  pause
  exit /b 1
)

echo [*] Ostanavlivayu staryy server na portu 8001 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8001 " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>nul

echo [*] Otkryvayu brauzer. ESLI STRANITSA PUSTAYA - obnovi ^(F5^) cherez 3-5 sek.
start "" "http://127.0.0.1:8001/"

echo.
echo [*] Zapuskayu server. NE ZAKRYVAY eto okno - ono i est server.
echo     Ostanovit: zakroy okno ili Ctrl+C.
echo -----------------------------------------------
echo.
"%VENVPY%" -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8001

echo.
echo ===============================================
echo [!] Server ostanovilsya ili upal s oshibkoy.
echo     Oshibka napisana VYSHE etoy stroki.
echo     Sdelay skrinshot vsego okna i prishli mne.
echo ===============================================
pause
