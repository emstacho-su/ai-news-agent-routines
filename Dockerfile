# syntax=docker/dockerfile:1.7
# AI News Agent container image.
#
# Single-stage build on python:3.11-slim with uv installed via pip.
# Multi-stage was tempting but the venv would have to ship with the
# right interpreter on the target image anyway, and `uv pip install`
# is fast enough that the marginal cost of a single-stage rebuild is
# small. Trades ~50MB of slim base for clearer ergonomics.
#
# Two entrypoints:
#   - default CMD runs uvicorn for the dashboard (Fly main process)
#   - `python main.py --run-daily --notify` is what the Fly scheduled
#     machine runs at 12:00 UTC daily (Phase 14 wires that up).

FROM python:3.11-slim

# uv: fast package installer + venv manager. We pin to a stable major.
# psutil/build deps are not needed; --system avoids the venv ceremony.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.5.* \
    && uv --version

# Non-root user. Fly volumes are typically chowned to UID 1000; we
# match so /app/data is writable when the volume mounts in.
ARG APP_UID=1000
RUN useradd --create-home --uid ${APP_UID} --shell /bin/bash agent

WORKDIR /app

# Install dependencies first so a code-only edit doesn't bust the layer.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# Copy application source. .dockerignore keeps tests/, .venv, .git
# out of the image so it stays slim.
COPY . .

# data/ is the runtime volume mount target. Pre-create + chown so the
# container can run as non-root even on first boot before Fly mounts.
RUN mkdir -p /app/data \
    && chown -R agent:agent /app
USER agent

EXPOSE 8080

# Health: hit /static/style.css since it's open to unauthenticated
# requests. Cheap and avoids embedding the dashboard password.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --max-time 4 http://127.0.0.1:8080/static/style.css \
        || exit 1

# Default process: dashboard. Override with `--entrypoint python main.py`
# (or via Fly's [processes] config) for the scheduled cron run.
CMD ["uvicorn", "dashboard:app", "--host", "0.0.0.0", "--port", "8080"]
