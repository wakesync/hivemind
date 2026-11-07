# swarm-memory: confidential shared memory API (CPU). Runs in a Tinfoil enclave.
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    EMBED_CACHE=/models HF_HUB_OFFLINE=0 \
    DB_PATH=/data/swarm.db WEB_DIR=/srv/web
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image: zero runtime downloads, and the
# weights are covered by the image digest that the enclave attests to.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/models')" \
 && chmod -R a+rX /models
ENV HF_HUB_OFFLINE=1

COPY app ./app
COPY web ./web

RUN useradd -u 10001 -m swarm && mkdir -p /data && chown swarm /data
USER 10001
EXPOSE 8080
HEALTHCHECK CMD curl -sf http://localhost:8080/healthz || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
