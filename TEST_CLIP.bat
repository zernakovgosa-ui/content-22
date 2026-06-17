@echo off
chcp 65001 >nul
REM ============================================================
REM   TREZZY - TEST narezki: peretashchi VIDEO na etot fayl.
REM   Sdelaet vertikalnye klipy 9:16 s subtitrami i otkroet papku.
REM   Server ne nuzhen - rabotaet napryamuyu cherez venv.
REM ============================================================
cd /d "%~dp0"
title TREZZY - Test narezki klipov
set "VENVPY=%~dp0trezzy-video-worker\.venv\Scripts\python.exe"
set "PYTHONPATH=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%VENVPY%" (
  echo [X] Okruzhenie venv ne naydeno.
  echo     Zapusti START.bat odin raz ^(sozdast venv 2-5 min^), potom snova syuda.
  echo.
  pause
  exit /b 1
)

echo ===============================================
echo   TREZZY - TEST narezki klipov
echo ===============================================
echo.
echo  Sposob 1: peretashchi videofayl pryamo na TEST_CLIP.bat
echo  Sposob 2: polozhi video v papku assets\source\ i zapusti
echo.

"%VENVPY%" "%~dp0tools\clip_test.py" %1 %2

echo.
echo -----------------------------------------------
echo  Esli klipov net - smotri soobshchenie vyshe.
echo  Dlya umnoy narezki + subtitrov nuzhen klyuch Groq
echo  ^(besplatno: console.groq.com^) v Nastroykah dashboarda.
echo -----------------------------------------------
pause
