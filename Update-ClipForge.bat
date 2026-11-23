@echo off
title ClipForge - Update + Start
cd /d "%~dp0"

echo ============================================
echo   ClipForge update ho raha hai...
echo ============================================

:: Purani chalti hui ClipForge band karo (port 8780 par jo bhi ho)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8780 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

:: Latest version GitHub se download karo
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri 'https://github.com/abh747393-creatord/ClipForge/archive/refs/heads/main.zip' -OutFile '%TEMP%\clipforge-update.zip' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 (
    echo Internet nahi mila - purana version hi chala rahi hun.
    goto :start_app
)

:: Extract karke isi folder par copy karo (.venv aur data ko haath nahi lagana)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%TEMP%\clipforge-update.zip' -DestinationPath '%TEMP%\clipforge-update' -Force"
robocopy "%TEMP%\clipforge-update\ClipForge-main" "%~dp0" /E /XD .venv data /XF "Update-ClipForge.bat" >nul
del "%TEMP%\clipforge-update.zip" >nul 2>&1
rmdir /s /q "%TEMP%\clipforge-update" >nul 2>&1
echo Update ho gaya!

:start_app
:: Pehli dafa ho to venv banao, warna requirements check kar lo
if not exist ".venv\Scripts\python.exe" (
    echo Pehli dafa setup ho raha hai, thoda time lagega...
    py -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
) else (
    .venv\Scripts\pip install -q -r requirements.txt >nul 2>&1
)

echo ClipForge start ho raha hai... browser khud khul jayega.
start "" /min powershell -NoProfile -Command "Start-Sleep -Seconds 5; Start-Process 'http://127.0.0.1:8780'"
.venv\Scripts\python -m app
pause
