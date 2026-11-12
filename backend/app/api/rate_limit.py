"""Limit case requests before reading the body or calling endpoint code."""

import sqlite3

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.db.rate_limits import RateLimitExceeded, consume_request, network_key
from backend.app.db.config import Database, StorageUnavailable


def request_category(path: str, method: str) -> str | None:
    path = path.rstrip("/")
    if method == "POST" and path == "/cases":
        return "create"
    if method == "POST" and path.endswith("/explanation"):
        return "explanation"
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return "evidence"
    return None


class RequestRateLimit:
    def __init__(self, app: ASGIApp, database: Database):
        self.app = app
        self.database = database

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/cases" or path.startswith("/cases/")):
            await self.app(scope, receive, send)
            return

        # Use the ASGI peer only. Never parse forwarding headers here.
        peer = scope.get("client")
        network = network_key(peer[0] if peer else None)
        try:
            await run_in_threadpool(
                consume_request, self.database, network,
                request_category(path, scope["method"]),
            )
        except RateLimitExceeded as error:
            response = JSONResponse(
                {"detail": str(error)}, status_code=error.status_code,
                headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        except (sqlite3.Error, StorageUnavailable):
            # A failed counter write must never silently bypass the budget.
            response = JSONResponse(
                {"detail": "Request limiting is temporarily unavailable. Try again shortly."},
                status_code=503,
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
