# syntax=docker/dockerfile:1.7
# SolutionForge API image. Build context: repository root.
#   docker build -f infra/docker/api.Dockerfile -t solutionforge-api .

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY apps/api/pyproject.toml ./
COPY apps/api/src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip && /opt/venv/bin/pip install .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY apps/api/alembic.ini ./
COPY apps/api/migrations ./migrations
COPY --chmod=755 infra/docker/api-start.sh /usr/local/bin/api-start
COPY --chmod=755 infra/docker/sf-env.sh /usr/local/bin/sf-env
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz').status != 200)"
ENTRYPOINT ["sf-env"]
CMD ["api-start"]
