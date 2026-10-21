FROM ghcr.io/astral-sh/uv:0.6.14 AS uv

FROM python:3.12-slim

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra langfuse --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --extra langfuse

EXPOSE 8080
CMD ["uvicorn", "llm_convo.api:app", "--host", "0.0.0.0", "--port", "8080"]
