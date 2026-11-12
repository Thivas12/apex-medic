"""Bound HTTP bodies before JSON parsing or application side effects."""

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.limits import BODY_DEADLINE_SECONDS, MAX_REQUEST_BYTES


class RequestBodyLimit:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_REQUEST_BYTES,
        deadline_seconds: float = BODY_DEADLINE_SECONDS,
    ):
        if max_bytes < 1 or deadline_seconds <= 0:
            raise ValueError("Body limits must be positive.")
        self.app = app
        self.max_bytes = max_bytes
        self.deadline_seconds = deadline_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status: int, detail: str) -> None:
            response = JSONResponse(
                {"detail": detail}, status_code=status,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)

        lengths = []
        for name, value in scope.get("headers", []):
            name = name.lower()
            if name == b"content-length":
                lengths.append(value.strip())
            if name == b"content-encoding" and value.strip().lower() != b"identity":
                await reject(415, "Compressed request bodies are not supported.")
                return

        if lengths:
            if len(lengths) != 1 or len(lengths[0]) > 20 or not lengths[0].isdigit():
                await reject(400, "Invalid Content-Length header.")
                return
            if int(lengths[0]) > self.max_bytes:
                await reject(413, f"Request body exceeds {self.max_bytes} bytes.")
                return

        body = bytearray()
        too_large = False
        try:
            async with asyncio.timeout(self.deadline_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        too_large = True
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(408, "Request body was not received in time.")
            return

        if too_large:
            await reject(413, f"Request body exceeds {self.max_bytes} bytes.")
            return

        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
