FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/srv/silent-relay/.venv/bin:$PATH"
WORKDIR /srv/silent-relay
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*
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
