@echo off
REM One-command launcher for the Rotation Dashboard (Windows).
cd /d "%~dp0backend"

echo Setting up Python environment...
if not exist ".venv" (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

set BUILD_FRONTEND=0
if not exist "..\frontend\dist\index.html" set BUILD_FRONTEND=1
if "%BUILD_FRONTEND%"=="0" (
  powershell -NoProfile -Command "$dist=(Get-Item '..\frontend\dist\index.html').LastWriteTime; $srcChanged=(Get-ChildItem '..\frontend\src' -Recurse -File | Where-Object { $_.LastWriteTime -gt $dist } | Select-Object -First 1); $pkgChanged=((Get-Item '..\frontend\package.json').LastWriteTime -gt $dist) -or ((Get-Item '..\frontend\package-lock.json').LastWriteTime -gt $dist); if ($srcChanged -or $pkgChanged) { exit 1 }"
  if errorlevel 1 set BUILD_FRONTEND=1
)

if "%BUILD_FRONTEND%"=="1" (
  echo Building frontend...
  cd ..\frontend
  call npm install --silent
  call npm run build
  cd ..\backend
)

echo Starting backend at http://localhost:5179
echo (Open that URL in your browser. Press Ctrl+C to stop.)
python app.py
