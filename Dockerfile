# ═══════════════════════════════════════════════════════════════
# FCB Bot — Production Container
# ═══════════════════════════════════════════════════════════════
# Lightweight Python image with only live-trading dependencies.
# Data dir, backtest, and analysis modules are NOT included —
# this image is purpose-built for 24/7 live execution.
#
# Build:  docker build -t fcb-bot .
# Run:    docker-compose up -d
# ═══════════════════════════════════════════════════════════════

FROM python:3.12-slim AS base

# ── System deps ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        # NTP for clock sync (critical for Bybit API timestamps)
        ntpsec-ntpdate \
        # Curl for healthcheck
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ──
RUN groupadd -r fcb && useradd -r -g fcb -m -d /app fcb

WORKDIR /app

# ── Python deps (cached layer) ──
COPY requirements-live.txt .
RUN pip install --no-cache-dir -r requirements-live.txt

# ── Application code ──
COPY live/ ./live/
COPY run_live.py .
COPY watchdog.py .
COPY dashboard.py .

# ── Persistent data directories ──
# These should be mounted as Docker volumes for persistence
RUN mkdir -p /app/live/logs && chown -R fcb:fcb /app

# ── Switch to non-root ──
USER fcb

# ── Environment (override via docker-compose or .env) ──
ENV BYBIT_API_KEY="" \
    BYBIT_API_SECRET="" \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    DASHBOARD_PORT=8080

# ── Expose dashboard port ──
EXPOSE 8080

# ── Healthcheck — verify watchdog is alive ──
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "watchdog.py" > /dev/null || exit 1

# ── Entrypoint ──
# Sync clock before starting (handles drift that killed STBL trail)
# Then run the watchdog which supervises the bot with auto-restart
ENTRYPOINT ["/bin/sh", "-c", "ntpdate -s pool.ntp.org 2>/dev/null; exec python watchdog.py"]
