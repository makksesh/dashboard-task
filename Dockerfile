FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

CMD ["sh", "-c", "python -m mvp_task.cli init && exec uvicorn mvp_task.web:app --host 0.0.0.0 --port 8501"]
