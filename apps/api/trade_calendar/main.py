import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.responses import Response

from trade_calendar import __version__
from trade_calendar.api import (
    calendar_router,
    changes_router,
    events_router,
    notifications_router,
    settings_router,
    sources_router,
)
from trade_calendar.core.config import get_settings
from trade_calendar.core.database import engine
from trade_calendar.core.errors import ApiError
from trade_calendar.core.logging import configure_logging
from trade_calendar.source_registry import seed_sources
from trade_calendar.tokens import ensure_ics_token

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info({"event": "api_started", "environment": settings.environment})
    try:
        from trade_calendar.core.database import SessionLocal

        async with SessionLocal() as session:
            await seed_sources(session, settings)
            await ensure_ics_token(session, settings)
    except Exception:
        logger.exception({"event": "source_seed_failed"})
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Request-ID"],
)

INTERNAL_API_SECRET_HEADER = "X-Internal-Api-Secret"
# /health and /ready stay open so the Tunnel and local tooling can probe the
# process, and /calendar/* keeps its own unguessable path token -- ICS clients
# cannot send custom headers, so gating that route would break subscriptions.
INTERNAL_API_SECRET_EXEMPT_PATHS = frozenset({"/health", "/ready"})
INTERNAL_API_SECRET_EXEMPT_PREFIXES = ("/calendar/",)


@app.middleware("http")
async def require_internal_api_secret(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject callers that do not hold the shared secret.

    The API itself has no user accounts; it relies on network isolation, so a
    public Tunnel would otherwise expose every route -- including writes -- to
    anyone who learns the hostname. When ``internal_api_secret`` is configured
    the secret becomes the gate. Unset means "gate disabled", which is the
    local development and unit test default.
    """
    secret = settings.internal_api_secret
    # A blank value counts as "not configured". Otherwise an empty
    # INTERNAL_API_SECRET in .env would look like a working gate while actually
    # accepting every request that simply omits the header.
    if secret is None or not secret.get_secret_value().strip():
        return await call_next(request)
    path = request.url.path
    if path in INTERNAL_API_SECRET_EXEMPT_PATHS or path.startswith(
        INTERNAL_API_SECRET_EXEMPT_PREFIXES
    ):
        return await call_next(request)
    supplied = request.headers.get(INTERNAL_API_SECRET_HEADER, "")
    if not secrets.compare_digest(
        supplied.encode("utf-8"), secret.get_secret_value().encode("utf-8")
    ):
        logger.warning({"event": "internal_api_secret_rejected", "path": path})
        return JSONResponse(
            status_code=401,
            content={"error": {
                "code": "unauthorized",
                "message": "缺少或无效的内部 API 凭据",
                "request_id": getattr(request.state, "request_id", "unknown"),
            }},
        )
    return await call_next(request)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("X-Request-ID", str(uuid4()))[:100]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {
            "code": exc.code, "message": exc.message,
            "request_id": getattr(request.state, "request_id", "unknown"),
        }},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "validation_error", "message": "请求数据校验失败",
            "request_id": getattr(request.state, "request_id", "unknown"),
            "details": jsonable_encoder(exc.errors(), custom_encoder={ValueError: str}),
        }},
    )


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {
        "status": "ok",
        "service": "api",
        "version": __version__,
        "request_id": request.state.request_id,
    }


@app.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok", "request_id": request.state.request_id}


app.include_router(events_router, prefix="/api/v1")
app.include_router(changes_router, prefix="/api/v1")
app.include_router(sources_router, prefix="/api/v1")
app.include_router(notifications_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")
app.include_router(calendar_router)
