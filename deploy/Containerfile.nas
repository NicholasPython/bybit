FROM localhost/bybitbot-runtime:0.1.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY config/config.example.yaml ./config/config.example.yaml
ENTRYPOINT ["python", "-m", "bybitbot.cli"]
CMD ["--config", "/run/bybitbot/config.yaml", "run"]
