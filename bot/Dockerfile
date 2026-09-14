# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

# Fail fast and log immediately; a buffered crash loop tells you nothing.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code changes do not invalidate the layer.
COPY requirements.txt .
COPY constraints.txt .
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

# Only the runtime modules. `.dockerignore` excludes tests, tools, docs and —
# critically — every .env file. The predecessor of this image shipped live API
# keys inside it.
# The list is the static import closure of main.py (tests/test_dockerfile.py
# recomputes it and fails the build if a module is missing). The previous list
# omitted market_data.py, memory.py, project_status.py and session_log.py, all
# imported unconditionally, so `python3 main.py` died on ModuleNotFoundError
# before the health server bound and the container restart-looped.
COPY bybit_connection.py carry_broker.py carry_costs.py carry_engine.py \
     carry_risk.py config.py features.py ledger.py main.py market_data.py \
     market_snapshot.py memory.py ml_strategy.py persistence.py \
     policy.py position_sizing.py project_status.py promotion_gate.py \
     pure_indicators.py risk_management.py session_log.py shadow.py \
     technical_analysis.py trading_engine.py ./
COPY signals/ ./signals/

# The operator tools, named ONE BY ONE — never `COPY tools/`, which would put
# every script and fetcher in the image.
#
#   connector_check   can THIS host reach the venue? The only question worth
#                     asking before a drill.
#   session_tail      the four invariants, printed from inside the image.
#   drill             Phase D itself (0045). Reads and reports without --arm.
#   fund_demo         puts BTC in a DEMO wallet (0046), so an overlay has
#                     something to hedge.
#
# 0049: the first two were shipped and the last two were not, while
# scripts/deploy.sh told the operator to run all four inside the container.
# The instruction failed at the one moment it mattered.
# tests/test_image_ships_the_tools.py compares the two lists now.
COPY tools/connector_check.py tools/session_tail.py \
     tools/drill.py tools/fund_demo.py ./tools/

# Run unprivileged. State lives on a mounted volume owned by this user.
RUN useradd --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app/state && chown -R bot:bot /app
USER bot

# Durable state MUST be a volume. Losing it means losing peak equity, daily
# anchors, breaker state and open positions — every brake the bot has.
VOLUME ["/app/state"]
ENV STATE_DB_PATH=/app/state/trading_state.db

# Safe by default. Going live requires all four of USE_TESTNET=0,
# PAPER_TRADING=0, LIVE_TRADING_ACK=I_UNDERSTAND and real credentials,
# supplied at runtime from a secret manager — never baked in here.
ENV USE_TESTNET=1 \
    PAPER_TRADING=1 \
    ENABLE_HEALTH_SERVER=1 \
    HEALTHCHECK_PORT=8081 \
    LOG_LEVEL=INFO

EXPOSE 8081

# Generous start period: startup reconciliation runs before the server binds,
# and killing the container mid-reconciliation is how amnesia loops begin.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=4).status==200 else 1)"

# Exec form: SIGTERM reaches Python, so graceful shutdown actually runs.
ENTRYPOINT ["python3", "main.py"]
