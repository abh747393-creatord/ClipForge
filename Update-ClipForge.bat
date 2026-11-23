@echo off
title ClipForge - Update + Start
cd /d "%~dp0"

echo ============================================
echo   ClipForge update ho raha hai...
echo ============================================

:: Abhi wala version dikhao
set OLDVER=unknown
for /f "tokens=3 delims= " %%v in ('findstr /c:"VERSION =" app\server.py 2^>nul') do set OLDVER=%%~v
echo Abhi installed version: %OLDVER%

:: Purani chalti hui ClipForge band karo (port 8780 par jo bhi ho)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8780 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1
timeout /t 2 /nobreak >nul

:: Purani temp files saaf karo, phir latest version download karo
del "%TEMP%\clipforge-update.zip" >nul 2>&1
rmdir /s /q "%TEMP%\clipforge-update" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri 'https://github.com/abh747393-creatord/ClipForge/archive/refs/heads/main.zip' -OutFile '%TEMP%\clipforge-update.zip' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 goto :dlfail
if not exist "%TEMP%\clipforge-update.zip" goto :dlfail

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%TEMP%\clipforge-update.zip' -DestinationPath '%TEMP%\clipforge-update' -Force"
if not exist "%TEMP%\clipforge-update\ClipForge-main\app\server.py" goto :dlfail

:: Download mein konsa version aaya hai, check karo
set NEWVER=unknown
for /f "tokens=3 delims= " %%v in ('findstr /c:"VERSION =" "%TEMP%\clipforge-update\ClipForge-main\app\server.py"') do set NEWVER=%%~v
echo Download mein version: %NEWVER%

:: Pehle se latest hai to copy ki zaroorat nahi
if "%OLDVER%"=="%NEWVER%" (
    echo Pehle se latest version hai - copy skip kar rahi hun.
    del "%TEMP%\clipforge-update.zip" >nul 2>&1
    rmdir /s /q "%TEMP%\clipforge-update" >nul 2>&1
    goto :start_app
)

:: Isi folder par copy karo (.venv aur data ko haath nahi lagana)
robocopy "%TEMP%\clipforge-update\ClipForge-main" "%~dp0" /E /XD .venv data __pycache__ /XF "Update-ClipForge.bat" /R:2 /W:1 > "%TEMP%\clipforge-copy.log"

:: Purana bytecode saaf karo taake purana code na chale
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path '%~dp0app' -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"

del "%TEMP%\clipforge-update.zip" >nul 2>&1
rmdir /s /q "%TEMP%\clipforge-update" >nul 2>&1

:: Verify: folder mein ab naya version hai?
set NOWVER=unknown
for /f "tokens=3 delims= " %%v in ('findstr /c:"VERSION =" app\server.py 2^>nul') do set NOWVER=%%~v
echo Update ke baad version: %NOWVER%
if not "%NOWVER%"=="%NEWVER%" goto :copyfail
echo Update pakka ho gaya! Version %NOWVER%
goto :start_app

:dlfail
echo ============================================
echo   DOWNLOAD FAIL ho gaya - internet check karo.
echo   Purana version %OLDVER% hi chala rahi hun.
echo ============================================
goto :start_app

:copyfail
echo ============================================
echo   COPY FAIL ho gaya - files replace nahi huin!
echo   ClipForge ki saari windows band karke ye file
echo   dobara chalao. Log: %TEMP%\clipforge-copy.log
echo ============================================
pause
goto :start_app

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
