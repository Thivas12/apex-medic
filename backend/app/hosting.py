"""Explicit Render deployment configuration; local development stays separate."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from backend.app.api.access import validate_public_origin
from backend.app.api.render_ingress import RenderIngress
from backend.app.db.config import (
    DatabaseConfigurationError, PostgresDatabase,
)
from backend.app.model_config import ModelConfigurationError, load_model_settings


class HostingConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class RenderSettings:
    origin: str
    allowed_hosts: tuple[str, ...]
    database: PostgresDatabase
    port: int


def render_settings() -> RenderSettings:
    if os.environ.get("RENDER") != "true" or os.environ.get("RENDER_SERVICE_TYPE") != "web":
        raise HostingConfigurationError("This startup command is only for a Render web service.")

    try:
        render_origin = validate_public_origin(os.environ.get("RENDER_EXTERNAL_URL", ""))
        render_host = urlsplit(render_origin).hostname
        if not render_origin.startswith("https://") or not render_host.endswith(".onrender.com"):
            raise ValueError
        origin = validate_public_origin(os.environ.get("APEX_PUBLIC_ORIGIN") or render_origin)
        hostname = urlsplit(origin).hostname
        if not origin.startswith("https://") or hostname in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError
    except ValueError:
        raise HostingConfigurationError("Set a valid public HTTPS origin; Render must provide RENDER_EXTERNAL_URL.") from None

    if os.environ.get("APEX_DATABASE_BACKEND", "postgres") != "postgres":
        raise HostingConfigurationError("The Render service requires PostgreSQL, not local SQLite.")
    if not os.environ.get("DATABASE_URL"):
        raise HostingConfigurationError("DATABASE_URL is required in the Render environment.")
    try:
        # Deliberately omit the migration credential from the runtime profile.
        database = PostgresDatabase(os.environ["DATABASE_URL"])
    except DatabaseConfigurationError:
        raise HostingConfigurationError("DATABASE_URL must be a valid PostgreSQL URL using TLS for a remote host.") from None

    if os.environ.get("APEX_LLM_PROVIDER") != "groq" or not os.environ.get("GROQ_API_KEY"):
        raise HostingConfigurationError("Set APEX_LLM_PROVIDER=groq and GROQ_API_KEY in the Render environment.")
    try:
        load_model_settings()
    except ModelConfigurationError:
        raise HostingConfigurationError("The Groq configuration is invalid. Check the server environment settings.") from None

    try:
        port = int(os.environ.get("PORT", "10000"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise HostingConfigurationError("PORT must be an integer between 1 and 65535.") from None
    return RenderSettings(origin, tuple(dict.fromkeys([hostname, render_host])), database, port)


def create_render_app():
    settings = render_settings()
    from backend.app.api.main import create_app
    application = create_app(settings.database, public_origin=settings.origin)
    # Outermost: normalize the edge-supplied address before rate limiting.
    application.add_middleware(RenderIngress, allowed_hosts=settings.allowed_hosts)
    return application
