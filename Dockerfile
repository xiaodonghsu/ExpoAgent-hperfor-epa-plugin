FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HPERFOR_PLUGIN_CONFIG=/data/config/config.json \
    LOG_FILE=/data/logs/hperfor-epa-plugin.log \
    LOG_LEVEL=INFO

WORKDIR /app

COPY pyproject.toml uv.lock README.md main.py ./

RUN pip install --no-cache-dir uv \
    && uv pip install --system .

RUN mkdir -p /data/config /data/logs

VOLUME ["/data/config", "/data/logs"]

CMD ["python", "main.py"]
