# =============================================================================
# Visual Database Designer module — standalone Module Protocol v1 service
# (Phase 6F, fourth expert module — database design).
# Build context MUST be the repository root (it needs packages/module-sdk-python):
#   docker build -f services/modules/visual-database-designer/Dockerfile -t visual-database-designer .
# =============================================================================
FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install the SDK first (changes rarely → better layer caching).
COPY packages/module-sdk-python /sdk
RUN pip install --no-cache-dir /sdk

# Module deps + code.
COPY services/modules/visual-database-designer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/modules/visual-database-designer/app /app/app
# The drag & drop canvas (served at /canvas for the interactive designer).
COPY services/modules/visual-database-designer/frontend /app/frontend

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 9107

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9107/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9107"]
