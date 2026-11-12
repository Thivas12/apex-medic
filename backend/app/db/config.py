"""Select storage without logging or exporting connection credentials."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SQLITE = PROJECT_ROOT / "data" / "private" / "cases.sqlite3"


class DatabaseConfigurationError(ValueError):
    pass


class StorageUnavailable(RuntimeError):
    """Only a fixed, credential-free message may cross the storage boundary."""

    def __init__(self):
        super().__init__("Case storage is unavailable. Try again shortly.")


def connection_parts(value: str, *, direct: bool = False) -> dict[str, str]:
    try:
        uri = urlsplit(value)
        if uri.scheme not in {"postgres", "postgresql"}:
            raise ValueError
        parts = conninfo_to_dict(value)
        host = parts.get("host", "")
        if not host or "," in host or not parts.get("dbname") or not parts.get("user"):
            raise ValueError
        if host not in {"127.0.0.1", "localhost", "::1"}:
            if parts.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
                raise ValueError
        if direct and "-pooler." in host:
            raise ValueError
        return parts
    except Exception:
        # Parser exceptions can contain the original secret. Never propagate them.
        label = "DATABASE_URL_UNPOOLED" if direct else "DATABASE_URL"
        raise DatabaseConfigurationError(
            f"{label} must be a valid PostgreSQL URL with TLS for remote hosts. "
            "Migrations require the direct, unpooled endpoint."
        ) from None


@dataclass(frozen=True)
class PostgresDatabase:
    url: str = field(repr=False)
    direct_url: str | None = field(default=None, repr=False)

    def __post_init__(self):
        connection_parts(self.url)

    def migration_database(self) -> "PostgresDatabase":
        if not self.direct_url:
            raise DatabaseConfigurationError(
                "DATABASE_URL_UNPOOLED is required to run migrations."
            )
        app = connection_parts(self.url)
        direct = connection_parts(self.direct_url, direct=True)
        # Neon pooled and direct hosts differ only by the '-pooler' suffix.
        for key in ("host", "port", "dbname", "user"):
            left = app.get(key, "5432" if key == "port" else "")
            right = direct.get(key, "5432" if key == "port" else "")
            if key == "host":
                left = left.replace("-pooler.", ".")
            if left != right:
                raise DatabaseConfigurationError(
                    "The pooled and direct URLs must refer to the same database and role."
                )
        return PostgresDatabase(self.direct_url)


Database = Path | PostgresDatabase


def resolve_database(
    explicit: Database | None = None,
    *,
    env_file: Path | None = None,
) -> Database:
    if explicit is not None:
        # Tests and local admin tools can explicitly select an isolated SQLite file.
        return explicit if isinstance(explicit, PostgresDatabase) else Path(explicit)
    path = PROJECT_ROOT / ".env.local" if env_file is None else env_file
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig") if path.is_file() else {}

    def setting(name: str):
        return os.environ[name] if name in os.environ else values.get(name)

    backend = setting("APEX_DATABASE_BACKEND") or "auto"
    if backend not in {"auto", "sqlite", "postgres"}:
        raise DatabaseConfigurationError("APEX_DATABASE_BACKEND must be auto, sqlite, or postgres.")
    if backend == "sqlite":
        return DEFAULT_SQLITE
    url = setting("DATABASE_URL")
    if url:
        return PostgresDatabase(url, setting("DATABASE_URL_UNPOOLED"))
    if backend == "postgres" or "DATABASE_URL" in os.environ or "DATABASE_URL" in values:
        raise DatabaseConfigurationError("DATABASE_URL is required and must not be empty.")
    return DEFAULT_SQLITE
