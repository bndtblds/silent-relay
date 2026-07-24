FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/silent-relay
RUN addgroup --system silentrelay && adduser --system --ingroup silentrelay silentrelay
COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .
RUN mkdir -p /data && chown -R silentrelay:silentrelay /data /srv/silent-relay
USER silentrelay
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1", "--no-access-log"]
