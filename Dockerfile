# ==============================================================================
# p8 Unified Dockerfile
# Supports multiple entry points: API, Worker, CLI
# Built with uv for fast, deterministic builds
# ==============================================================================
#
# Build and Push (Multi-Platform with buildx):
#   VERSION=$(grep '^version' pyproject.toml | cut -d'"' -f2) && \
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t percolationlabs/p8:latest \
#     -t percolationlabs/p8:$(git rev-parse --short HEAD) \
#     -t percolationlabs/p8:$VERSION \
#     --push \
#     -f Dockerfile .
#
#   # Load locally for testing (single platform):
#   docker buildx build --platform linux/arm64 \
#     -t percolationlabs/p8:latest \
#     --load \
#     -f Dockerfile .
#
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Builder - Install dependencies with uv
# ------------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install build dependencies for packages with native extensions (Rust, C)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Disable bytecode compilation to avoid timeout on large files
ENV UV_COMPILE_BYTECODE=0

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Copy source code (needed for package installation)
COPY p8/ ./p8/

# Install dependencies and the p8 package into .venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ------------------------------------------------------------------------------
# Stage 2: Runtime - Minimal production image
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Install minimal runtime dependencies
# curl: health checks
# procps: process monitoring
# ca-certificates: SSL/TLS connections
# tesseract-ocr + eng: OCR engine for PDF parsing (Kreuzberg)
# ffmpeg: Audio/video processing (pydub)
# git + openssh-client: GitProvider for versioned schema syncing
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    procps \
    ca-certificates \
    tesseract-ocr \
    tesseract-ocr-eng \
    ffmpeg \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user for security
RUN useradd -m -u 1000 -s /bin/bash p8 && \
    chown -R p8:p8 /app

# Copy virtual environment from builder
COPY --from=builder --chown=p8:p8 /app/.venv /app/.venv

# Copy source code from builder
COPY --from=builder --chown=p8:p8 /app/p8 /app/p8

# Copy SQL init scripts
COPY --chown=p8:p8 sql/ /app/sql/

# Create Kreuzberg cache directory with write permissions
RUN mkdir -p /app/.kreuzberg && chown 1000:0 /app/.kreuzberg && chmod 775 /app/.kreuzberg

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONWARNINGS="ignore::SyntaxWarning:pydub,ignore::DeprecationWarning:pydub,ignore::DeprecationWarning:audioop"

# Switch to non-root user
USER p8

# Expose API port
EXPOSE 8000

# ------------------------------------------------------------------------------
# Entry Points - Override with docker-compose or kubernetes
# ------------------------------------------------------------------------------

# Default: API server with hypercorn (HTTP/2 support)
# Override with:
#   - CLI: ["p8", "migrate"]
CMD ["python", "-W", "ignore::SyntaxWarning", "-W", "ignore::DeprecationWarning", "-m", "hypercorn", "p8.api.main:app", "--bind", "0.0.0.0:8000", "--access-logfile", "/dev/null"]

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
