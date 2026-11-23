@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  ClipForge - Mint YouTube PO Token
echo  Fixes: "Sign in to confirm you're not a bot"
echo ============================================
echo.

where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed on this PC.
  echo Install the LTS version from https://nodejs.org then run this file again.
  echo.
  pause
  exit /b 1
)

if not exist "tools\node_modules" (
  echo First run: installing the token tool ^(one time, needs internet^)...
  pushd tools
  call npm install --no-audit --no-fund
  if errorlevel 1 (
    echo.
    echo npm install failed. Check your internet connection and try again.
    popd
    pause
    exit /b 1
  )
  popd
)

echo Minting a fresh PO token...
node tools\mint-pot.mjs
if errorlevel 1 (
  echo.
  echo Minting failed. Wait a minute and run this file again.
  pause
  exit /b 1
)

echo.
echo Done. ClipForge will use the token automatically - no restart needed.
echo The token lasts about 12 hours. Run this file again if the bot check returns.
echo.
pause
