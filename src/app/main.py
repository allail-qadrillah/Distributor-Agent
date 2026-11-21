import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.conversations import router as conversations_router
from app.api.outbox import router as outbox_router
from app.api.whatsapp import router as whatsapp_router
from app.observability import (
    clear_correlation_id,
    log_event,
    normalize_correlation_id,
    set_correlation_id,
)

_log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    from app.observability import configure_logging

    configure_logging()
    app = FastAPI(title="Distributor Operations Agent")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(whatsapp_router)
    app.include_router(conversations_router)
    app.include_router(outbox_router)

    @app.middleware("http")
    async def correlation_and_request_logging(request: Request, call_next) -> object:
        correlation_id = normalize_correlation_id(
            request.headers.get("x-correlation-id")
        )
        set_correlation_id(correlation_id)
        log_event(
            _log,
            "http_request_received",
            level=logging.DEBUG,
            method=request.method,
            path=request.url.path,
        )
        started = time.monotonic()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # log and answer: the correlation header must reach the
                # caller even on an unhandled error (re-raising would let
                # the outer error middleware build a header-less 500)
                log_event(
                    _log,
                    "http_request_completed",
                    level=logging.ERROR,
                    method=request.method,
                    path=request.url.path,
                    status=500,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    outcome="error",
                    error_type=type(exc).__name__,
                    exc=exc,
                )
                response = JSONResponse(
                    status_code=500, content={"detail": "internal_error"}
                )
                response.headers["X-Correlation-ID"] = correlation_id
                return response
            response.headers["X-Correlation-ID"] = correlation_id
            status = response.status_code
            if status < 400:
                outcome, level = "success", logging.INFO
            elif status < 500:
                outcome, level = "client_error", logging.WARNING
            else:
                outcome, level = "error", logging.ERROR
            log_event(
                _log,
                "http_request_completed",
                level=level,
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome,
            )
            return response
        finally:
            # defensive: never leak this request's correlation id into a
            # later request that reuses this task context
            clear_correlation_id()

    return app


app = create_app()
