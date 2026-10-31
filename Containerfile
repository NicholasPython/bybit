FROM python:3.12.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY config/config.example.yaml ./config/config.example.yaml
RUN uv sync --frozen --no-dev --no-editable
ENV PATH="/opt/venv/bin:$PATH"
ENTRYPOINT ["bybitbot"]
CMD ["--config", "/run/bybitbot/config.yaml", "run"]
