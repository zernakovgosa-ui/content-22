@echo off
title TREZZY STOP
echo Ostanavlivayu TREZZY server ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>nul
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8001 " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>nul
taskkill /F /IM python.exe >nul 2>nul
echo [+] Server ostanovlen.
timeout /t 2 /nobreak >nul
exit /b 0
