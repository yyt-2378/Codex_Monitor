FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_MONITOR_ENV=production \
    CODEX_MONITOR_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

EXPOSE 8080
VOLUME ["/data"]
CMD ["codex-monitor", "serve", "--host", "0.0.0.0", "--port", "8080"]

