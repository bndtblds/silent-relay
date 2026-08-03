FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/srv/silent-relay/.venv/bin:$PATH"
WORKDIR /srv/silent-relay
RUN addgroup --system silentrelay && adduser --system --ingroup silentrelay silentrelay
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-editable
RUN mkdir -p /data && chown -R silentrelay:silentrelay /data /srv/silent-relay
USER silentrelay
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1", "--no-access-log"]
