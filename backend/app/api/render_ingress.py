"""Render Free public-edge adapter; do not enable on a directly exposed server.

Render documents that Cloudflare overwrites CF-Connecting-IP. X-Forwarded-For
can retain caller-supplied prefixes, so this adapter deliberately ignores it.
See docs/render_deployment.md for the deployment boundary and sources.
"""

from ipaddress import ip_address

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RenderIngress:
    def __init__(self, app: ASGIApp, *, allowed_hosts: tuple[str, ...]):
        self.app = app
        self.allowed_hosts = set(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Render's internal health probes need not traverse Cloudflare. This
        # exception grants access only to the side-effect-free liveness endpoint.
        if scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return

        async def reject(code: int, detail: str) -> None:
            await JSONResponse(
                {"detail": detail}, status_code=code,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)

        headers = scope.get("headers", [])
        hosts = [value.lower() for key, value in headers if key.lower() == b"host"]
        if len(hosts) != 1 or hosts[0].decode("latin-1").removesuffix(":443") not in self.allowed_hosts:
            await reject(400, "Request host is not allowed.")
            return

        addresses = [value for key, value in headers if key.lower() == b"cf-connecting-ip"]
        try:
            if len(addresses) != 1 or not 1 <= len(addresses[0]) <= 45:
                raise ValueError
            value = addresses[0].decode("ascii")
            if "%" in value:
                raise ValueError
            address = str(ip_address(value))
        except (ValueError, UnicodeError):
            # Do not fall back to a spoofable forwarding header or shared proxy.
            await reject(503, "The hosting edge could not identify this request. Try again shortly.")
            return

        forwarded = dict(scope)
        forwarded["client"] = (address, 0)
        forwarded["scheme"] = "https"
        await self.app(forwarded, receive, send)
