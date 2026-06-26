# Build the React frontend, then run the Flask backend that serves the built assets.
FROM node:20-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt gunicorn

COPY backend/ ./backend/
# Root-level sector universe (read at startup by sector_data.py via REPO_DIR).
COPY tickers_by_sector.txt ./tickers_by_sector.txt
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

EXPOSE 8080

# Threaded worker so a synchronous /api/ingest?wait=1 run (cron trigger) does
# not block dashboard requests; long timeout so the run isn't killed mid-cycle.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --chdir backend --threads 8 --timeout 600 app:app"]
