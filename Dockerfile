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
# Human-facing version string, read at runtime by version.py via REPO_DIR.
COPY VERSION ./VERSION
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Build identity, surfaced at /api/version. Pass on deploy to pin the exact
# build, e.g. `fly deploy --build-arg GIT_SHA=$(git rev-parse --short HEAD)
# --build-arg BUILD_TIME=$(date -u +%FT%TZ)`. Absent → version.py degrades to
# the VERSION file alone (the runtime image has no .git to fall back to).
ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV APP_GIT_SHA=$GIT_SHA \
    APP_BUILD_TIME=$BUILD_TIME

EXPOSE 8080

# Threaded worker so a synchronous /api/ingest?wait=1 run (cron trigger) does
# not block dashboard requests; long timeout so the run isn't killed mid-cycle.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --chdir backend --threads 8 --timeout 600 app:app"]
