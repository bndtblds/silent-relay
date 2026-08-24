FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS runtime
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
