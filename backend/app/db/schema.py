"""Explicit, transactional migrations. App startup only checks the schema."""

import hashlib
from pathlib import Path

from backend.app.db.config import DatabaseConfigurationError, PostgresDatabase
from backend.app.db.postgres import RATE_LOCK, transaction


MIGRATION = Path(__file__).with_name("migrations") / "001_initial.sql"


def migration_text() -> str:
    # read_text normalizes CRLF, keeping the checksum stable on Windows.
    return MIGRATION.read_text(encoding="utf-8")


def _expected():
    return [(1, hashlib.sha256(migration_text().encode("utf-8")).hexdigest())]


def _check(connection):
    exists = connection.execute("SELECT to_regclass('apexmedic.schema_migrations')").fetchone()[0]
    if exists is None:
        raise DatabaseConfigurationError(
            "PostgreSQL schema is missing. Run: python -m backend.app.db.manage migrate"
        )
    rows = connection.execute(
        "SELECT version, checksum FROM apexmedic.schema_migrations ORDER BY version"
    ).fetchall()
    if rows != _expected():
        raise DatabaseConfigurationError("PostgreSQL schema version or migration checksum does not match this app.")
    if connection.execute("SELECT 1 FROM apexmedic.storage_totals WHERE id = 1").fetchone() is None:
        raise DatabaseConfigurationError("PostgreSQL storage accounting is missing.")


def check_schema(database: PostgresDatabase) -> None:
    with transaction(database) as connection:
        _check(connection)


def migrate(database: PostgresDatabase) -> bool:
    direct = database.migration_database()
    with transaction(direct, write=True) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (RATE_LOCK,))
        exists = connection.execute("SELECT to_regnamespace('apexmedic')").fetchone()[0]
        if exists is not None:
            _check(connection)
            return False
        connection.execute("CREATE SCHEMA apexmedic")
        connection.execute(
            "CREATE TABLE apexmedic.schema_migrations ("
            "version integer PRIMARY KEY, checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        connection.execute(migration_text(), prepare=False)
        connection.execute(
            "INSERT INTO apexmedic.schema_migrations (version, checksum) VALUES (%s, %s)",
            _expected()[0],
        )
        _check(connection)
    return True
