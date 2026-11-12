"""PostgreSQL storage; every mutation is a short, explicit transaction.

Transaction advisory locks preserve the SQLite implementation's serialized
quota/check/write behavior across server processes. They work with Neon's
transaction pooler and are released by commit or rollback, including failures.
"""

import hashlib
import hmac
import json
import math
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg

from backend.app.db import quotas
from backend.app.db.config import PostgresDatabase, StorageUnavailable
from backend.app.limits import (
    WriteLimitExceeded, check_case_capacity, check_new_case, check_new_entry,
)
from backend.app.models.case import Case, CompletedTest, Observation, SymptomAnswer


CASE_LOCK = 1095781697
RATE_LOCK = 1095781698


@contextmanager
def transaction(database: PostgresDatabase, *, write: bool = False, rates: bool = False):
    try:
        # Disable automatic prepared statements for transaction-pool compatibility.
        with psycopg.connect(database.url, connect_timeout=10, prepare_threshold=None) as connection:
            if write:
                connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
            else:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '15s'")
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL idle_in_transaction_session_timeout = '20s'")
            connection.execute("SET LOCAL TIME ZONE 'UTC'")
            if write:
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (RATE_LOCK if rates else CASE_LOCK,))
            yield connection
    except psycopg.Error:
        # Driver messages can contain credentials, SQL, or player data.
        raise StorageUnavailable() from None


def _storage_budget(connection, *, added_cases=0, added_bytes=0):
    row = connection.execute(
        "SELECT case_count, payload_bytes FROM apexmedic.storage_totals WHERE id = 1"
    ).fetchone()
    if row is None:
        raise StorageUnavailable()
    if added_cases > 0 and row[0] + added_cases > quotas.MAX_CASES:
        raise WriteLimitExceeded(
            "The service has reached its case capacity. Existing cases remain available.", 503,
        )
    if row[1] + added_bytes > quotas.MAX_PAYLOAD_BYTES:
        raise WriteLimitExceeded(
            "The service has reached its evidence storage capacity. Nothing new was saved.", 503,
        )


def _session_budget(connection, digest, *, new_session):
    if new_session:
        count = connection.execute("SELECT COUNT(*) FROM apexmedic.visitor_sessions").fetchone()[0]
        if count >= quotas.MAX_SESSIONS:
            raise WriteLimitExceeded(
                "The service has reached its browser-session capacity. Existing access is unchanged.", 503,
            )
    else:
        count = connection.execute(
            "SELECT COUNT(*) FROM apexmedic.case_owners WHERE session_hash = %s", (digest,),
        ).fetchone()[0]
        if count >= quotas.MAX_CASES_PER_SESSION:
            raise WriteLimitExceeded(
                f"This browser session already has {quotas.MAX_CASES_PER_SESSION} cases. "
                "Reopen an existing case to continue.", 409,
            )


def create_case(database, case):
    validated = Case.model_validate_json(case.model_dump_json())
    payload = validated.model_dump_json()
    check_new_case(validated, payload)
    with transaction(database, write=True) as connection:
        if connection.execute(
            "SELECT 1 FROM apexmedic.cases WHERE case_id = %s", (validated.case_id,),
        ).fetchone():
            raise ValueError(f"Case '{validated.case_id}' already exists.")
        _storage_budget(connection, added_cases=1, added_bytes=len(payload.encode("utf-8")))
        connection.execute(
            "INSERT INTO apexmedic.cases (case_id, payload) VALUES (%s, %s)",
            (validated.case_id, payload),
        )


def get_case(database, case_id):
    with transaction(database) as connection:
        row = connection.execute(
            "SELECT payload FROM apexmedic.cases WHERE case_id = %s", (case_id,),
        ).fetchone()
        return None if row is None else Case.model_validate_json(row[0])


def append_evidence(database, case_id, event_type, entry_json, event_id):
    models = {
        "observation": (Observation, "observations"),
        "test": (CompletedTest, "completed_tests"),
        "answer": (SymptomAnswer, None),
    }
    if event_type not in models:
        raise ValueError("Unsupported evidence type.")
    event_id = event_id.strip()
    if not event_id or len(event_id) > 100:
        raise ValueError("Event ID must contain 1 to 100 characters.")
    model, field = models[event_type]
    entry = model.model_validate_json(entry_json)
    payload = entry.model_dump_json()
    with transaction(database, write=True) as connection:
        previous = connection.execute(
            "SELECT case_id, event_type, payload FROM apexmedic.case_events WHERE event_id = %s",
            (event_id,),
        ).fetchone()
        if previous is not None:
            if previous != (case_id, event_type, payload):
                raise ValueError("Event ID already belongs to different evidence.")
            return False
        check_new_entry(entry)
        row = connection.execute(
            "SELECT payload FROM apexmedic.cases WHERE case_id = %s", (case_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Case '{case_id}' was not found.")
        case = Case.model_validate_json(row[0])
        if case.status != "investigating":
            raise ValueError("Only investigating cases can receive new evidence.")
        data = case.model_dump(mode="json")
        if event_type == "answer":
            if case.symptom != "unknown":
                raise ValueError("This case is not awaiting symptom classification.")
            data["symptom"] = entry.symptom
        else:
            data[field].append(entry.model_dump(mode="json"))
        updated = Case.model_validate_json(json.dumps(data))
        updated_payload = updated.model_dump_json()
        check_case_capacity(updated, updated_payload)
        _storage_budget(connection, added_bytes=(
            len(updated_payload.encode("utf-8")) - len(row[0].encode("utf-8"))
            + len(payload.encode("utf-8"))
        ))
        connection.execute(
            "UPDATE apexmedic.cases SET payload = %s WHERE case_id = %s",
            (updated_payload, case_id),
        )
        connection.execute(
            "INSERT INTO apexmedic.case_events "
            "(event_id, case_id, event_type, recorded_at, payload) VALUES (%s, %s, %s, %s, %s)",
            (event_id, case_id, event_type, datetime.now(timezone.utc), payload),
        )
    return True


def get_history(database, case_id):
    with transaction(database) as connection:
        if connection.execute(
            "SELECT 1 FROM apexmedic.cases WHERE case_id = %s", (case_id,),
        ).fetchone() is None:
            raise ValueError(f"Case '{case_id}' was not found.")
        rows = connection.execute(
            "SELECT event_id, event_type, recorded_at, payload "
            "FROM apexmedic.case_events WHERE case_id = %s ORDER BY sequence", (case_id,),
        ).fetchall()
    return [dict(event_id=r[0], event_type=r[1], recorded_at=r[2].isoformat(),
                 entry=json.loads(r[3])) for r in rows]


def create_owned_case(database, case, cookie_token, *, now=None):
    from backend.app.db.ownership import SESSION_SECONDS, VisitorSession, _token_hash

    validated = Case.model_validate_json(case.model_dump_json())
    payload = validated.model_dump_json()
    check_new_case(validated, payload)
    timestamp = int(time.time()) if now is None else now
    digest = _token_hash(cookie_token)
    with transaction(database, write=True) as connection:
        active = connection.execute(
            "SELECT expires_at FROM apexmedic.visitor_sessions WHERE token_hash = %s AND expires_at > %s",
            (digest, timestamp),
        ).fetchone()
        _storage_budget(connection, added_cases=1, added_bytes=len(payload.encode("utf-8")))
        _session_budget(connection, digest, new_session=active is None)
        if active is None:
            token = secrets.token_urlsafe(32)
            digest = _token_hash(token)
            expires_at = timestamp + SESSION_SECONDS
            connection.execute(
                "INSERT INTO apexmedic.visitor_sessions (token_hash, created_at, expires_at) VALUES (%s, %s, %s)",
                (digest, timestamp, expires_at),
            )
        else:
            token, expires_at = cookie_token, active[0]
        connection.execute(
            "INSERT INTO apexmedic.cases (case_id, payload) VALUES (%s, %s)",
            (validated.case_id, payload),
        )
        connection.execute(
            "INSERT INTO apexmedic.case_owners (case_id, session_hash) VALUES (%s, %s)",
            (validated.case_id, digest),
        )
    return VisitorSession(token=token, expires_at=expires_at)


def owns_case(database, case_id, cookie_token, *, now=None):
    from backend.app.db.ownership import _token_hash

    digest = _token_hash(cookie_token)
    if digest is None:
        return False
    timestamp = int(time.time()) if now is None else now
    with transaction(database) as connection:
        return connection.execute(
            "SELECT 1 FROM apexmedic.case_owners AS owners "
            "JOIN apexmedic.visitor_sessions AS sessions ON sessions.token_hash = owners.session_hash "
            "WHERE owners.case_id = %s AND sessions.token_hash = %s AND sessions.expires_at > %s",
            (case_id, digest, timestamp),
        ).fetchone() is not None


def _snapshot(connection, case_id, digest, now):
    from backend.app.db.case_deletion import CaseNotAvailable, DeletionPreview

    timestamp = int(time.time()) if now is None else now
    row = connection.execute(
        "SELECT cases.payload FROM apexmedic.cases AS cases "
        "JOIN apexmedic.case_owners AS owners ON owners.case_id = cases.case_id "
        "JOIN apexmedic.visitor_sessions AS sessions ON sessions.token_hash = owners.session_hash "
        "WHERE cases.case_id = %s AND sessions.token_hash = %s AND sessions.expires_at > %s",
        (case_id, digest, timestamp),
    ).fetchone()
    if row is None:
        raise CaseNotAvailable("Case not found.")
    case = Case.model_validate_json(row[0])
    fingerprint = hashlib.sha256()

    def include(value):
        fingerprint.update(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        fingerprint.update(b"\n")

    include([case_id, row[0]])
    rows = connection.execute(
        "SELECT sequence, event_id, event_type, recorded_at, payload "
        "FROM apexmedic.case_events WHERE case_id = %s ORDER BY sequence", (case_id,),
    ).fetchall()
    for sequence, event_id, event_type, recorded_at, payload in rows:
        include([sequence, event_id, event_type, recorded_at.isoformat(), payload])
    return DeletionPreview(
        case_id=case_id, player_description=case.player_description,
        origin=case.origin, status=case.status,
        observation_count=len(case.observations), test_result_count=len(case.completed_tests),
        history_event_count=len(rows), deletion_token=fingerprint.hexdigest(),
    )


def preview_deletion(database, case_id, cookie_token, *, now=None):
    from backend.app.db.case_deletion import CaseNotAvailable
    from backend.app.db.ownership import _token_hash

    digest = _token_hash(cookie_token)
    if digest is None:
        raise CaseNotAvailable("Case not found.")
    with transaction(database) as connection:
        return _snapshot(connection, case_id, digest, now)


def delete_owned_case(database, case_id, cookie_token, deletion_token, *, now=None):
    from backend.app.db.case_deletion import CaseChanged, CaseNotAvailable
    from backend.app.db.ownership import _token_hash

    digest = _token_hash(cookie_token)
    if digest is None:
        raise CaseNotAvailable("Case not found.")
    with transaction(database, write=True) as connection:
        current = _snapshot(connection, case_id, digest, now)
        if not hmac.compare_digest(current.deletion_token, deletion_token):
            raise CaseChanged("The case changed. Review it again before deleting.")
        connection.execute("DELETE FROM apexmedic.case_events WHERE case_id = %s", (case_id,))
        connection.execute("DELETE FROM apexmedic.case_owners WHERE case_id = %s", (case_id,))
        connection.execute("DELETE FROM apexmedic.cases WHERE case_id = %s", (case_id,))
        connection.execute(
            "DELETE FROM apexmedic.visitor_sessions WHERE token_hash = %s "
            "AND NOT EXISTS (SELECT 1 FROM apexmedic.case_owners WHERE session_hash = %s)",
            (digest, digest),
        )


def consume_request(database, network, category, *, now=None):
    from backend.app.db.rate_limits import GENERAL, POLICIES, MAX_COUNTERS, RateLimitExceeded

    timestamp = time.time() if now is None else now
    budgets = (*GENERAL, *POLICIES.get(category, ()))
    with transaction(database, write=True, rates=True) as connection:
        connection.execute("DELETE FROM apexmedic.request_counters WHERE expires_at <= %s", (timestamp,))
        pending, retry, missing = [], 0, 0
        for budget in budgets:
            key = budget.name + ":" + (network if budget.name.startswith("network") else "all")
            row = connection.execute(
                "SELECT hits, expires_at FROM apexmedic.request_counters WHERE bucket = %s", (key,),
            ).fetchone()
            if row is None:
                missing += 1
                pending.append((key, 1, timestamp + budget.seconds))
            else:
                if row[0] >= budget.limit:
                    retry = max(retry, math.ceil(row[1] - timestamp))
                pending.append((key, row[0] + 1, row[1]))
        if retry:
            raise RateLimitExceeded(retry)
        count = connection.execute("SELECT COUNT(*) FROM apexmedic.request_counters").fetchone()[0]
        if count + missing > MAX_COUNTERS:
            raise RateLimitExceeded(60, capacity=True)
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO apexmedic.request_counters (bucket, hits, expires_at) VALUES (%s, %s, %s) "
                "ON CONFLICT(bucket) DO UPDATE SET hits = excluded.hits, expires_at = excluded.expires_at",
                pending,
            )
