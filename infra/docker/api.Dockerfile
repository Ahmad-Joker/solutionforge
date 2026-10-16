# syntax=docker/dockerfile:1.7
# SolutionForge API image. Build context: repository root.
#   docker build -f infra/docker/api.Dockerfile -t solutionforge-api .

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY apps/api/pyproject.toml ./
COPY apps/api/src ./src
# Optional extras, e.g. --build-arg API_EXTRAS="[semantic]" for the bge-small embedder
# (adds ONNX Runtime; see docs/ASSISTANT_BENCHMARKS.md for its measured memory cost).
ARG API_EXTRAS=""
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip     && /opt/venv/bin/pip install ".${API_EXTRAS}"

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY apps/api/alembic.ini ./
COPY apps/api/migrations ./migrations
# Fictional demo corpus for the assistant (POST /orgs/{org}/assistant/demo-setup).
COPY corpus/harbor-pine ./corpus/harbor-pine
ENV SF_CORPUS_DIR=/app/corpus/harbor-pine
COPY --chmod=755 infra/docker/api-start.sh /usr/local/bin/api-start
COPY --chmod=755 infra/docker/sf-env.sh /usr/local/bin/sf-env
# The app runs as a non-root user whose home is not writable, so model caches must live
# elsewhere. With the semantic extra, bake the embedding model into the image at build
# time: no ~65 MB download on every deploy or cold start, and no runtime write access.
ARG API_EXTRAS=""
ENV HF_HOME=/tmp/huggingface SF_EMBEDDING_CACHE_DIR=/opt/models
RUN mkdir -p /opt/models     && if python -c "import fastembed" 2>/dev/null; then          python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/models')";        fi     && chown -R app:app /opt/models
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz').status != 200)"
ENTRYPOINT ["sf-env"]
CMD ["api-start"]
