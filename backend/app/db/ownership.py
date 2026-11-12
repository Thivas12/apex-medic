"""Browser ownership storage. Existing CLI repository functions remain local/admin."""

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field

from backend.app.models.case import Case
from backend.app.limits import check_new_case
from backend.app.db.quotas import check_session_budget, check_storage_budget
from backend.app.db.config import Database, PostgresDatabase


SESSION_SECONDS = 30 * 24 * 60 * 60
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class VisitorSession:
    token: str = field(repr=False)
    expires_at: int


def _token_hash(token: str | None) -> str | None:
    if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def initialise_ownership(database_path: Database) -> None:
    """Add tables; never claim or rewrite existing cases."""
    if isinstance(database_path, PostgresDatabase):
        # The explicit PostgreSQL migration creates all storage tables together.
        return
    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS visitor_sessions (
                    token_hash TEXT PRIMARY KEY NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS case_owners (
                    case_id TEXT PRIMARY KEY NOT NULL
                        REFERENCES cases(case_id),
                    session_hash TEXT NOT NULL
                        REFERENCES visitor_sessions(token_hash)
                )
                """
            )

            connection.execute(
                "CREATE INDEX IF NOT EXISTS owners_by_session ON case_owners(session_hash)"
            )


def create_owned_case(
    database_path: Database,
    case: Case,
    cookie_token: str | None,
    *,
    now: int | None = None,
) -> VisitorSession:
    """Create session (if needed), case and owner in one transaction."""
    if isinstance(database_path, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.create_owned_case(database_path, case, cookie_token, now=now)
    validated = Case.model_validate_json(case.model_dump_json())
    payload = validated.model_dump_json()
    check_new_case(validated, payload)
    current_time = int(time.time()) if now is None else now
    digest = _token_hash(cookie_token)

    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT expires_at FROM visitor_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (digest, current_time),
            ).fetchone()

            check_storage_budget(
                connection, added_cases=1, added_bytes=len(payload.encode("utf-8")),
            )
            check_session_budget(connection, digest, new_session=active is None)

            if active is None:
                token = secrets.token_urlsafe(32)
                digest = _token_hash(token)
                expires_at = current_time + SESSION_SECONDS
                connection.execute(
                    "INSERT INTO visitor_sessions "
                    "(token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                    (digest, current_time, expires_at),
                )
            else:
                token = cookie_token
                expires_at = active[0]

            connection.execute(
                "INSERT INTO cases (case_id, payload) VALUES (?, ?)",
                (validated.case_id, payload),
            )
            connection.execute(
                "INSERT INTO case_owners (case_id, session_hash) VALUES (?, ?)",
                (validated.case_id, digest),
            )

    return VisitorSession(token=token, expires_at=expires_at)


def owns_case(
    database_path: Database,
    case_id: str,
    cookie_token: str | None,
    *,
    now: int | None = None,
) -> bool:
    if isinstance(database_path, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.owns_case(database_path, case_id, cookie_token, now=now)
    digest = _token_hash(cookie_token)
    if digest is None:
        return False

    current_time = int(time.time()) if now is None else now
    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        row = connection.execute(
            "SELECT 1 FROM case_owners AS owners "
            "JOIN visitor_sessions AS sessions "
            "ON sessions.token_hash = owners.session_hash "
            "WHERE owners.case_id = ? AND sessions.token_hash = ? "
            "AND sessions.expires_at > ?",
            (case_id, digest, current_time),
        ).fetchone()
    return row is not None
