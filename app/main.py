from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.config import get_settings
from app.database import engine
from app.entitlements import (
    EntitlementProviderConfigurationError, load_entitlement_provider,
)
from app.i18n import (
    LANGUAGE_LABELS, SUPPORTED_LANGUAGES, browser_language, translate,
)
from app.routers import admin, web

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger("silent_relay")


def page_context(request: Request, **values: object) -> dict[str, object]:
    language = browser_language(request, settings.default_language)
    return {
        "request": request,
        "language": language,
        "supported_languages": SUPPORTED_LANGUAGES,
        "language_labels": LANGUAGE_LABELS,
        "t": lambda key, **arguments: translate(language, key, **arguments),
        **values,
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "severity": record.levelname,
            "event": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        return json.dumps(payload)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)
logger.setLevel(settings.log_level)


@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        application.state.entitlement_provider = load_entitlement_provider(
            settings.entitlement_provider
        )
    except EntitlementProviderConfigurationError as exc:
        logger.error("entitlement_provider_startup_failed: %s", exc)
        raise
    logger.info("entitlement_provider_loaded: %s", settings.entitlement_provider)
    yield


app = FastAPI(
    title="SilentRelay",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(web.router)
app.include_router(admin.router)

_requests: dict[str, deque[float]] = defaultdict(deque)
_token_path = re.compile(r"^/(account|notify|verify-contact)/[^/]+")


@app.middleware("http")
async def protection(request: Request, call_next):
    request_id = secrets.token_hex(16)
    now = time.monotonic()
    peer = request.client.host if request.client else "unknown"
    key = f"{peer}:{request.url.path}"
    bucket = _requests[key]
    while bucket and bucket[0] < now - settings.rate_limit_window_seconds:
        bucket.popleft()
    limit = settings.rate_limit_default
    if request.method == "POST" and len(bucket) >= limit:
        return templates.TemplateResponse(
            request,
            "error.html",
            page_context(
                request,
                title=translate(browser_language(request, settings.default_language), "error.rate_title"),
                message=translate(browser_language(request, settings.default_language), "error.rate_message"),
            ),
            status_code=429,
        )
    bucket.append(now)
    try:
        response = await call_next(request)
    except Exception:
        logging.getLogger("silent_relay").exception("unhandled_request", extra={"request_id": request_id})
        raise
    response.headers["X-Request-ID"] = request_id
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    if settings.hsts_enabled:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception):
    return templates.TemplateResponse(
        request,
        "error.html",
        page_context(
            request,
            title=translate(browser_language(request, settings.default_language), "error.not_found_title"),
            message=translate(browser_language(request, settings.default_language), "error.not_found_message"),
        ),
        status_code=404,
    )
