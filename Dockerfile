FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    ANONYMIZED_TELEMETRY=False \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY rag/ rag/

# Embute o modelo de embeddings na imagem (sem download em runtime)
RUN python -c "from rag.core import embedder; embedder()"

COPY mcp_server.py .
COPY data/index/ data/index/

RUN useradd --create-home --uid 1000 app && mkdir -p /app/state && chown -R app:app /app
USER app

ENV HF_HUB_OFFLINE=1 \
    MCP_TRANSPORT=http \
    MCP_STATE_DIR=/app/state \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"

CMD ["python", "mcp_server.py"]
