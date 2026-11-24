"""Logging, request ids and error handling for the API (step 8c).

  NUDGEIQ_LOG_LEVEL      DEBUG, INFO (default), WARNING, ERROR or CRITICAL
  NUDGEIQ_CORS_ORIGINS   comma-separated origins allowed to call the API from a browser.
                         Unset (default): no CORS headers. The Vite proxy and the nginx in
                         docker-compose make the browser talk to one origin, so none are needed.

What this adds to every request:
  * an X-Request-ID header (a safe incoming one is reused, otherwise a new one is made);
  * one log line: method, path, status, milliseconds, request id;
  * an unexpected error becomes a JSON 500 {"detail", "request_id"} and the traceback is logged,
    so a stack trace never reaches the client;
  * a lost database connection becomes a JSON 503 instead of a 500.
"""

import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

LOGGER_NAME = "nudgeiq"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
QUIET_PATHS = frozenset({"/health"})  # health checks poll often; log them at DEBUG only
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HANDLER_MARK = "_nudgeiq_handler"

log = logging.getLogger(LOGGER_NAME)


def log_level_from_env(env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    raw = (source.get("NUDGEIQ_LOG_LEVEL") or "").strip().upper()
    if not raw:
        return logging.INFO
    if raw not in LOG_LEVELS:
        raise ValueError(f"NUDGEIQ_LOG_LEVEL is {raw!r}; choose one of {list(LOG_LEVELS)}")
    return int(logging.getLevelName(raw))


def configure_logging(env: Mapping[str, str] | None = None) -> None:
    """Set the 'nudgeiq' logger up once. Safe to call again; it only updates the level."""
    log.setLevel(log_level_from_env(env))
    if any(getattr(h, _HANDLER_MARK, False) for h in log.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    setattr(handler, _HANDLER_MARK, True)
    log.addHandler(handler)


def cors_origins_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    source = os.environ if env is None else env
    raw = source.get("NUDGEIQ_CORS_ORIGINS") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _request_id(request: Request) -> str:
    incoming = request.headers.get("x-request-id", "")
    return incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]


def install_hardening(app: FastAPI, env: Mapping[str, str] | None = None) -> None:
    configure_logging(env)

    @app.exception_handler(psycopg.OperationalError)
    async def _database_down(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        log.error("database unavailable (request %s): %s", rid, exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "database unavailable", "request_id": rid},
            headers={"X-Request-ID": rid},
        )

    @app.middleware("http")
    async def _requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = _request_id(request)
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "unhandled error (request %s) %s %s", rid, request.method, request.url.path
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "internal server error", "request_id": rid},
            )
        ms = (time.perf_counter() - started) * 1000
        level = logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO
        log.log(
            level,
            "%s %s -> %s in %.0f ms (request %s)",
            request.method,
            request.url.path,
            response.status_code,
            ms,
            rid,
        )
        response.headers["X-Request-ID"] = rid
        return response

    origins = cors_origins_from_env(env)
    if origins:  # added last, so it is the outermost layer and also covers error responses
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )
