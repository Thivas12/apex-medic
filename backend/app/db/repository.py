import sqlite3
import json
from contextlib import closing
from datetime import datetime, timezone

from backend.app.models.case import Case, Observation, CompletedTest, SymptomAnswer
from backend.app.limits import check_case_capacity, check_new_case, check_new_entry
from backend.app.db.quotas import check_storage_budget, initialise_quotas
from backend.app.db.config import Database, PostgresDatabase


def initialise_database(database_path: Database) -> None:
    if isinstance(database_path, PostgresDatabase):
        from backend.app.db.schema import check_schema
        return check_schema(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            _ensure_history(connection)
            initialise_quotas(connection)


def create_case(database_path: Database, case: Case) -> None:
    if isinstance(database_path, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.create_case(database_path, case)
    # Revalidate at the storage boundary before writing.
    validated = Case.model_validate_json(case.model_dump_json())
    payload = validated.model_dump_json()
    check_new_case(validated, payload)

    try:
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                check_storage_budget(
                    connection, added_cases=1, added_bytes=len(payload.encode("utf-8")),
                )
                connection.execute(
                    "INSERT INTO cases (case_id, payload) VALUES (?, ?)",
                    (
                        validated.case_id,
                        payload,
                    ),
                )
    except sqlite3.IntegrityError as error:
        raise ValueError(
            f"Case '{validated.case_id}' already exists."
        ) from error


def get_case(database_path: Database, case_id: str) -> Case | None:
    if isinstance(database_path, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.get_case(database_path, case_id)
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            "SELECT payload FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()

    if row is None:
        return None

    return Case.model_validate_json(row[0])

def _ensure_history(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS case_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL REFERENCES cases(case_id),
            event_type TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )


def append_evidence(
    database_path: Database,
    case_id: str,
    event_type: str,
    entry_json: str,
    event_id: str,
) -> bool:
    if isinstance(database_path, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.append_evidence(database_path, case_id, event_type, entry_json, event_id)
    models = {
        "observation": (Observation, "observations"),
        "test": (CompletedTest, "completed_tests"),
        "answer": (SymptomAnswer, None)
    }

    if event_type not in models:
        raise ValueError("Unsupported evidence type.")

    event_id = event_id.strip()
    if not event_id or len(event_id) > 100:
        raise ValueError("Event ID must contain 1 to 100 characters.")

    model, field = models[event_type]
    entry = model.model_validate_json(entry_json)
    payload = entry.model_dump_json()

    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")

        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_history(connection)

            previous = connection.execute(
                "SELECT case_id, event_type, payload FROM case_events "
                "WHERE event_id = ?",
                (event_id,),
            ).fetchone()

            if previous is not None:
                if previous != (case_id, event_type, payload):
                    raise ValueError(
                        "Event ID already belongs to different evidence."
                    )
                return False

            # Exact retries return above, even when a legacy case is over quota.
            check_new_entry(entry)

            row = connection.execute(
                "SELECT payload FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()

            if row is None:
                raise ValueError(f"Case '{case_id}' was not found.")

            case = Case.model_validate_json(row[0])

            if case.status != "investigating":
                raise ValueError(
                    "Only investigating cases can receive new evidence."
                )

            data = case.model_dump(mode="json")

            if event_type == "answer":
                if case.symptom != "unknown":
                    raise ValueError(
                        "This case is not awaiting symptom classification."
                    )

                data["symptom"] = entry.symptom

            else:
                data[field].append(entry.model_dump(mode="json"))

            updated = Case.model_validate_json(json.dumps(data))
            updated_payload = updated.model_dump_json()
            check_case_capacity(updated, updated_payload)
            check_storage_budget(
                connection,
                added_bytes=(
                    len(updated_payload.encode("utf-8"))
                    - len(row[0].encode("utf-8"))
                    + len(payload.encode("utf-8"))
                ),
            )

            connection.execute(
                "UPDATE cases SET payload = ? WHERE case_id = ?",
                (updated_payload, case_id),
            )

            connection.execute(
                "INSERT INTO case_events "
                "(event_id, case_id, event_type, recorded_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event_id,
                    case_id,
                    event_type,
                    datetime.now(timezone.utc).isoformat(),
                    payload,
                ),
            )

    return True


def get_history(database_path: Database, case_id: str) -> list[dict]:
    if isinstance(database_path, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.get_history(database_path, case_id)
    if get_case(database_path, case_id) is None:
        raise ValueError(f"Case '{case_id}' was not found.")

    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            _ensure_history(connection)

        rows = connection.execute(
            "SELECT event_id, event_type, recorded_at, payload "
            "FROM case_events WHERE case_id = ? ORDER BY sequence",
            (case_id,),
        ).fetchall()

    return [
        {
            "event_id": row[0],
            "event_type": row[1],
            "recorded_at": row[2],
            "entry": json.loads(row[3]),
        }
        for row in rows
    ]
