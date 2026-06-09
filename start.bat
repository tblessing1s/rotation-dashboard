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

if not exist "..\frontend\dist" (
  echo Building frontend ^(first run^)...
  cd ..\frontend
  call npm install --silent
  call npm run build
  cd ..\backend
)

echo Starting backend at http://localhost:5179
echo (Open that URL in your browser. Press Ctrl+C to stop.)
python app.py
