# =============================================================================
# external-dns - Universal DNS Synchronization
# =============================================================================
# Lightweight Python container for syncing reverse proxy routes to DNS providers
# Supports multiple DNS and reverse proxy implementations via provider plugins
# =============================================================================

FROM python:3.12-slim

# Install runtime dependencies (from the packaged project)
WORKDIR /app

COPY pyproject.toml README.md LICENSE /app/
COPY src/ /app/src/

RUN pip install --no-cache-dir .

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash syncer

# (No script copy needed; entrypoint is installed via console_scripts)

# Switch to non-root user
USER syncer

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)"

# Run the sync script
CMD ["external-dns"]
