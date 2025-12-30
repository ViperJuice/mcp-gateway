# syntax=docker/dockerfile:1

# Build stage - install dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast dependency installation
RUN pip install --no-cache-dir uv

# Copy dependency files
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install the package and dependencies
RUN uv pip install --system .

# Runtime stage - minimal image
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/pmcp /usr/local/bin/

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

# Set environment defaults
ENV PMCP_LOG_LEVEL=info
ENV PYTHONUNBUFFERED=1

# Health check placeholder (MCP uses stdio, not HTTP)
# HEALTHCHECK --interval=30s --timeout=3s CMD echo "ok"

ENTRYPOINT ["pmcp"]
