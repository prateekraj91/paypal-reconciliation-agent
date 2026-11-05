# Production multi-stage Dockerfile for PayPal Reconciliation Agent
# Builds a secure, minimal container supporting both API web service and background worker.

FROM python:3.12-slim AS builder

WORKDIR /app

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project specification
COPY pyproject.toml .
COPY src/ src/

# Install application and production dependencies into a virtual environment
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir "/app[store,api]"

# --------------------------------------------------------------------------
# Runtime Stage
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Install libpq for PostgreSQL connectivity
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-privileged service user
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g appuser -s /bin/false -m appuser

WORKDIR /app

# Copy virtualenv and application from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
COPY alembic.ini .

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

# Healthcheck targeting public /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run as non-root user
USER appuser

EXPOSE 8000

# Default command starts the FastAPI web service; worker service overrides CMD with:
# python -m reconcile_worker
CMD ["uvicorn", "reconcile_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
