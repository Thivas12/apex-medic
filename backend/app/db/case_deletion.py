"""Owner-authorized, version-checked deletion from the active database."""

import hashlib
import hmac
import json
import sqlite3
import time
from contextlib import closing

from backend.app.db.ownership import _token_hash
from backend.app.models.case import Case, ValidatedModel
from backend.app.db.config import Database, PostgresDatabase


class CaseNotAvailable(ValueError):
    pass


class CaseChanged(ValueError):
    pass


class DeletionPreview(ValidatedModel):
    case_id: str
    player_description: str
    origin: str
    status: str
    observation_count: int
    test_result_count: int
    history_event_count: int
    deletion_token: str


def _snapshot(
    connection: sqlite3.Connection,
    case_id: str,
    digest: str | None,
    now: int | None,
) -> DeletionPreview:
    current_time = int(time.time()) if now is None else now
    row = connection.execute(
        "SELECT cases.payload FROM cases "
        "JOIN case_owners ON case_owners.case_id = cases.case_id "
        "JOIN visitor_sessions ON visitor_sessions.token_hash = case_owners.session_hash "
        "WHERE cases.case_id = ? AND visitor_sessions.token_hash = ? "
        "AND visitor_sessions.expires_at > ?",
        (case_id, digest, current_time),
    ).fetchone()
    if row is None:
        raise CaseNotAvailable("Case not found.")

    case = Case.model_validate_json(row[0])
    fingerprint = hashlib.sha256()

    def include(value):
        fingerprint.update(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        fingerprint.update(b"\n")

    include([case_id, row[0]])
    history_count = 0
    for event in connection.execute(
        "SELECT sequence, event_id, event_type, recorded_at, payload "
        "FROM case_events WHERE case_id = ? ORDER BY sequence", (case_id,),
    ):
        include(event)
        history_count += 1

    return DeletionPreview(
        case_id=case_id,
        player_description=case.player_description,
        origin=case.origin,
        status=case.status,
        observation_count=len(case.observations),
        test_result_count=len(case.completed_tests),
        history_event_count=history_count,
        deletion_token=fingerprint.hexdigest(),
    )


def preview_deletion(
    database: Database, case_id: str, cookie_token: str | None, *, now: int | None = None,
) -> DeletionPreview:
    if isinstance(database, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.preview_deletion(database, case_id, cookie_token, now=now)
    digest = _token_hash(cookie_token)
    if digest is None:
        raise CaseNotAvailable("Case not found.")
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        with connection:
            connection.execute("BEGIN")
            return _snapshot(connection, case_id, digest, now)


def delete_owned_case(
    database: Database,
    case_id: str,
    cookie_token: str | None,
    deletion_token: str,
    *,
    now: int | None = None,
) -> None:
    if isinstance(database, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.delete_owned_case(database, case_id, cookie_token, deletion_token, now=now)
    digest = _token_hash(cookie_token)
    if digest is None:
        raise CaseNotAvailable("Case not found.")
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            # Recheck ownership and the entire snapshot while holding the write lock.
            current = _snapshot(connection, case_id, digest, now)
            if not hmac.compare_digest(current.deletion_token, deletion_token):
                raise CaseChanged("The case changed. Review it again before deleting.")

            connection.execute("DELETE FROM case_events WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM case_owners WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
            connection.execute(
                "DELETE FROM visitor_sessions WHERE token_hash = ? "
                "AND NOT EXISTS (SELECT 1 FROM case_owners WHERE session_hash = ?)",
                (digest, digest),
            )
