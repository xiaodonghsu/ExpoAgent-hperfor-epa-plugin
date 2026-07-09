FROM python:3.12-slim

ENV UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
ENV PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
ENV PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn"

ENV HPERFOR_PLUGIN_CONFIG=/data/config/config.json \
    LOG_FILE=/data/logs/hperfor-epa-plugin.log \
    LOG_BACKUP_COUNT=30 \
    LOG_LEVEL=INFO

WORKDIR /app

COPY pyproject.toml uv.lock README.md main.py ./

RUN pip install --no-cache-dir uv \
    && uv pip install --system .

RUN mkdir -p /data/config /data/logs

VOLUME ["/data/config", "/data/logs"]

CMD ["python", "main.py"]
