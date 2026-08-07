from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import SessionLocal, engine
from app.entitlements import (
    EntitlementProviderConfigurationError, load_entitlement_provider,
)
from app.i18n import (
    LANGUAGE_LABELS, SUPPORTED_LANGUAGES, browser_language, translate,
)
from app.routers import admin, web
from app.rate_limit import PersistentRateLimiter, policy_for_path
from app.time import utc_now

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
            "timestamp": utc_now().isoformat().replace("+00:00", "Z"),
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

def protected_response(response: Response, request_id: str) -> Response:
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


def error_response(request: Request, status_code: int, title_key: str, message_key: str) -> Response:
    language = browser_language(request, settings.default_language)
    return templates.TemplateResponse(
        request,
        "error.html",
        page_context(
            request,
            title=translate(language, title_key),
            message=translate(language, message_key),
        ),
        status_code=status_code,
    )


@app.middleware("http")
async def protection(request: Request, call_next):
    request_id = secrets.token_hex(16)
    if request.method == "POST":
        policy, subject = policy_for_path(request.url.path, settings)
        peer = request.client.host if request.client else "unknown"
        try:
            with SessionLocal() as db:
                decision = PersistentRateLimiter(settings).check(
                    db, policy, peer, subject
                )
                db.commit()
        except SQLAlchemyError:
            logger.exception(
                "rate_limit_store_unavailable", extra={"request_id": request_id}
            )
            return protected_response(
                error_response(
                    request, 503, "error.unavailable_title", "error.unavailable_message"
                ),
                request_id,
            )
        if not decision.allowed:
            response = error_response(
                request, 429, "error.rate_title", "error.rate_message"
            )
            response.headers["Retry-After"] = str(decision.retry_after_seconds)
            return protected_response(response, request_id)
    try:
        response = await call_next(request)
    except Exception:
        logging.getLogger("silent_relay").exception("unhandled_request", extra={"request_id": request_id})
        raise
    return protected_response(response, request_id)


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
