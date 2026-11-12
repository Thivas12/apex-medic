"""Transactional quotas for new data; existing records are never removed."""

import sqlite3

from backend.app.limits import WriteLimitExceeded


MAX_CASES = 500
MAX_SESSIONS = 500
MAX_CASES_PER_SESSION = 20
MAX_PAYLOAD_BYTES = 100 * 1024 * 1024


def initialise_quotas(connection: sqlite3.Connection) -> None:
    """Call inside startup's write transaction after cases/history exist."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS storage_totals ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "case_count INTEGER NOT NULL, payload_bytes INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO storage_totals (id, case_count, payload_bytes) "
        "SELECT 1, (SELECT COUNT(*) FROM cases), "
        "COALESCE((SELECT SUM(length(CAST(payload AS BLOB))) FROM cases), 0) + "
        "COALESCE((SELECT SUM(length(CAST(payload AS BLOB))) FROM case_events), 0) "
        "ON CONFLICT(id) DO UPDATE SET case_count = excluded.case_count, "
        "payload_bytes = excluded.payload_bytes"
    )
    # Constants below are fixed table names, never user input.
    for table in ("cases", "case_events"):
        count = 1 if table == "cases" else 0
        for event, byte_change, count_change in (
            ("INSERT", "length(CAST(NEW.payload AS BLOB))", count),
            ("DELETE", "-length(CAST(OLD.payload AS BLOB))", -count),
            ("UPDATE OF payload", "length(CAST(NEW.payload AS BLOB)) - length(CAST(OLD.payload AS BLOB))", 0),
        ):
            name = event.split()[0].lower()
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS budget_{table}_{name} "
                f"AFTER {event} ON {table} BEGIN "
                f"UPDATE storage_totals SET case_count = case_count + ({count_change}), "
                f"payload_bytes = payload_bytes + ({byte_change}) WHERE id = 1; END"
            )


def check_storage_budget(
    connection: sqlite3.Connection,
    *,
    added_cases: int = 0,
    added_bytes: int = 0,
) -> None:
    # The caller holds BEGIN IMMEDIATE through the check and all writes.
    row = connection.execute(
        "SELECT case_count, payload_bytes FROM storage_totals WHERE id = 1"
    ).fetchone()
    if row is None:
        raise WriteLimitExceeded("Storage accounting is unavailable. Nothing was saved.", 503)
    if added_cases > 0 and row[0] + added_cases > MAX_CASES:
        raise WriteLimitExceeded(
            "The service has reached its case capacity. Existing cases remain available.", 503,
        )
    if row[1] + added_bytes > MAX_PAYLOAD_BYTES:
        raise WriteLimitExceeded(
            "The service has reached its evidence storage capacity. Nothing new was saved.", 503,
        )


def check_session_budget(
    connection: sqlite3.Connection, digest: str | None, *, new_session: bool,
) -> None:
    if new_session:
        count = connection.execute("SELECT COUNT(*) FROM visitor_sessions").fetchone()[0]
        if count >= MAX_SESSIONS:
            raise WriteLimitExceeded(
                "The service has reached its browser-session capacity. Existing access is unchanged.", 503,
            )
    else:
        count = connection.execute(
            "SELECT COUNT(*) FROM case_owners WHERE session_hash = ?", (digest,),
        ).fetchone()[0]
        if count >= MAX_CASES_PER_SESSION:
            raise WriteLimitExceeded(
                f"This browser session already has {MAX_CASES_PER_SESSION} cases. "
                "Reopen an existing case to continue.", 409,
            )
