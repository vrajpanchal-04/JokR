# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv==0.8.17

# Dependencies first so code edits don't bust the layer cache.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY config ./config
COPY jokr ./jokr
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home jokr
USER jokr

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status != 200)"]

# Migrations run in compose's one-shot `migrate` service, so the API never
# holds the owner credentials.
CMD ["uvicorn", "jokr.api.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Test image: same code plus dev tools and the test suite. Used by `make test`.
FROM base AS test
USER root
RUN uv sync --frozen
COPY tests ./tests
USER jokr
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
