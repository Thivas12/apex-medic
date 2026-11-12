"""Start the hosted app: python -m backend.app.serve. Not a local dev command."""

import uvicorn

from backend.app.hosting import HostingConfigurationError, render_settings


def main() -> int:
    try:
        settings = render_settings()
    except HostingConfigurationError as error:
        print(f"Hosting configuration: {error}")
        return 1
    uvicorn.run(
        "backend.app.hosting:create_render_app", factory=True,
        host="0.0.0.0", port=settings.port, workers=1, reload=False,
        proxy_headers=False, forwarded_allow_ips="",
        access_log=False, server_header=False,
        limit_concurrency=32, timeout_keep_alive=5,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
