#!/usr/bin/env bash
# One-command launcher for the Rotation Dashboard (macOS / Linux).
set -e
cd "$(dirname "$0")"

echo "▸ Setting up Python environment…"
cd backend
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# Build the frontend once if it hasn't been built
if [ ! -d "../frontend/dist" ]; then
  echo "▸ Building frontend (first run)…"
  cd ../frontend
  npm install --silent
  npm run build
  cd ../backend
fi

echo "▸ Starting backend at http://localhost:5179"
echo "  (Open that URL in your browser. Press Ctrl+C to stop.)"
python app.py
