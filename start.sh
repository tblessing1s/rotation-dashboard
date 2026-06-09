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

# Build the frontend if it hasn't been built, or if source changed after dist.
needs_frontend_build=0
if [ ! -f "../frontend/dist/index.html" ]; then
  needs_frontend_build=1
elif [ -n "$(find ../frontend/src ../frontend/package.json ../frontend/package-lock.json -newer ../frontend/dist/index.html -print -quit)" ]; then
  needs_frontend_build=1
fi

if [ "$needs_frontend_build" -eq 1 ]; then
  echo "▸ Building frontend…"
  cd ../frontend
  npm install --silent
  npm run build
  cd ../backend
fi

echo "▸ Starting backend at http://localhost:5179"
echo "  (Open that URL in your browser. Press Ctrl+C to stop.)"
python app.py
