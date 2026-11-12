"""Real PostgreSQL tests, opt-in on a disposable LOCAL test database only.

Never uses DATABASE_URL, DATABASE_URL_UNPOOLED, or .env.local. Each test drops
the apexmedic schema in APEX_TEST_POSTGRES_URL after checking the local host
and an apexmedic_test_ database prefix.
"""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

import test_api
from backend.app.api.main import create_app
from backend.app.db import quotas
from backend.app.db.case_deletion import CaseChanged, CaseNotAvailable, delete_owned_case, preview_deletion
from backend.app.db.config import DatabaseConfigurationError, PostgresDatabase, StorageUnavailable, connection_parts
from backend.app.db.ownership import create_owned_case, owns_case
from backend.app.db.postgres import transaction
from backend.app.db.rate_limits import RateLimitExceeded, consume_request
from backend.app.db.repository import append_evidence, create_case, get_case, get_history, initialise_database
from backend.app.db.schema import migrate
from backend.app.limits import WriteLimitExceeded
from backend.app.models.case import Case


TEST_URL = os.environ.get("APEX_TEST_POSTGRES_URL")
READING = json.dumps({"source": "player_report", "context": "Synthetic தமிழ் reading", "packet_loss_pct": 8})


def fresh_database():
    parts = connection_parts(TEST_URL)
    local = {"127.0.0.1", "localhost", "::1"}
    if (parts["host"] not in local
            or parts.get("hostaddr", "127.0.0.1") not in {"127.0.0.1", "::1"}
            or "service" in parts
            or not parts["dbname"].startswith("apexmedic_test_")):
        raise RuntimeError("PostgreSQL tests require a local database named apexmedic_test_... .")
    database = PostgresDatabase(TEST_URL, TEST_URL)
    with psycopg.connect(TEST_URL) as connection:
        connection.execute("DROP SCHEMA IF EXISTS apexmedic CASCADE")
    migrate(database)
    return database


def case(case_id="pg-case", **changes):
    return Case(case_id=case_id, origin="synthetic", platform="windows_pc",
                symptom="unknown", player_description="Synthetic PostgreSQL case",
                connection_type="ethernet", **changes)


@unittest.skipUnless(TEST_URL, "Set APEX_TEST_POSTGRES_URL for isolated local PostgreSQL tests")
class PostgresApiTests(test_api.ApiTests):
    # Run the SAME HTTP contract tests against both actual storage engines.
    def setUp(self):
        self.db = fresh_database()
        self.client = self.enterContext(TestClient(create_app(self.db)))

    def test_other_browser_cannot_read_modify_explain_or_delete(self):
        case_id = self.new_case()
        base = f"/cases/{case_id}"
        with TestClient(create_app(self.db)) as stranger:
            for suffix in ("", "/history", "/next-step", "/deletion-preview"):
                self.assertEqual(stranger.get(base + suffix).status_code, 404)
            self.assertEqual(stranger.post(base + "/observations", json={
                "event_id": "bad-access", "entry": json.loads(READING),
            }).status_code, 404)
            self.assertEqual(stranger.post(base + "/explanation", json={"decision_token": "a" * 64}).status_code, 404)
            self.assertEqual(stranger.request("DELETE", base, json={
                "confirm_case_id": case_id, "deletion_token": "a" * 64,
            }).status_code, 404)

    def test_owner_deletion_flow(self):
        case_id = self.new_case()
        base = f"/cases/{case_id}"
        self.submit(case_id, "observations", json.loads(READING), "pg-observation")
        preview = self.client.get(base + "/deletion-preview").json()
        response = self.client.request("DELETE", base, json={
            "confirm_case_id": case_id, "deletion_token": preview["deletion_token"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(base).status_code, 404)

    def test_storage_failure_returns_503_without_details(self):
        case_id = self.new_case()
        with patch("backend.app.db.postgres.psycopg.connect", side_effect=psycopg.OperationalError("SECRET")):
            response = self.client.get(f"/cases/{case_id}")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")


@unittest.skipUnless(TEST_URL, "Set APEX_TEST_POSTGRES_URL for isolated local PostgreSQL tests")
class PostgresStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = fresh_database()

    def totals(self):
        with transaction(self.db) as connection:
            return connection.execute("SELECT case_count, payload_bytes FROM apexmedic.storage_totals").fetchone()

    def test_migration_is_idempotent_and_preserves_data(self):
        create_case(self.db, case())
        self.assertFalse(migrate(self.db))
        initialise_database(self.db)
        self.assertIsNotNone(get_case(self.db, "pg-case"))

    def test_startup_rejects_missing_and_unknown_schema(self):
        with psycopg.connect(TEST_URL) as connection:
            connection.execute("UPDATE apexmedic.schema_migrations SET version = 999")
        with self.assertRaises(DatabaseConfigurationError):
            initialise_database(self.db)
        with psycopg.connect(TEST_URL) as connection:
            connection.execute("DROP SCHEMA apexmedic CASCADE")
        with self.assertRaises(DatabaseConfigurationError):
            initialise_database(self.db)

    def test_parallel_evidence_updates_and_exact_retries(self):
        create_case(self.db, case())
        def add(index):
            return append_evidence(self.db, "pg-case", "observation", READING, f"obs-{index}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(add, [0, 0, 1, 2, 3, 4, 5, 6])), 7)
        self.assertEqual(len(get_case(self.db, "pg-case").observations), 7)
        self.assertEqual(len(get_history(self.db, "pg-case")), 7)
        with self.assertRaises(ValueError):
            append_evidence(self.db, "pg-case", "observation", READING.replace(": 8", ": 9"), "obs-0")

    def test_competing_answers_cannot_overwrite(self):
        create_case(self.db, case())
        def answer(symptom):
            try:
                append_evidence(self.db, "pg-case", "answer", json.dumps({
                    "question_id": "clarify_symptom", "symptom": symptom,
                }), symptom)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(answer, ["packet_loss", "high_latency"])), 1)
        self.assertEqual(len(get_history(self.db, "pg-case")), 1)

    def test_history_failure_rolls_back_case_and_accounting(self):
        create_case(self.db, case())
        before = self.totals()
        with psycopg.connect(TEST_URL) as connection:
            connection.execute("ALTER TABLE apexmedic.case_events ADD CONSTRAINT reject_test CHECK (event_id <> 'fail')")
        with self.assertRaises(StorageUnavailable):
            append_evidence(self.db, "pg-case", "observation", READING, "fail")
        self.assertEqual(get_case(self.db, "pg-case").observations, [])
        self.assertEqual(get_history(self.db, "pg-case"), [])
        self.assertEqual(self.totals(), before)

    def test_global_case_and_byte_quota_with_concurrency(self):
        def add(index):
            try:
                create_case(self.db, case(f"case-{index}"))
                return True
            except WriteLimitExceeded:
                return False
        with patch.object(quotas, "MAX_CASES", 1), ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(add, range(4))), 1)
        self.assertEqual(self.totals()[0], 1)
        with patch.object(quotas, "MAX_PAYLOAD_BYTES", 0):
            with self.assertRaises(WriteLimitExceeded):
                create_case(self.db, case("no-space"))
        self.assertIsNone(get_case(self.db, "no-space"))

    def test_session_quota_and_last_case_deletion_releases_capacity(self):
        owner = create_owned_case(self.db, case(), None)
        with patch.object(quotas, "MAX_SESSIONS", 1):
            with self.assertRaises(WriteLimitExceeded):
                create_owned_case(self.db, case("other"), None)
        with patch.object(quotas, "MAX_CASES_PER_SESSION", 1):
            with self.assertRaises(WriteLimitExceeded):
                create_owned_case(self.db, case("same"), owner.token)
        preview = preview_deletion(self.db, "pg-case", owner.token)
        delete_owned_case(self.db, "pg-case", owner.token, preview.deletion_token)
        with patch.object(quotas, "MAX_SESSIONS", 1):
            create_owned_case(self.db, case("replacement"), None)

    def test_expired_session_and_legacy_case_are_not_owned(self):
        owner = create_owned_case(self.db, case(), None, now=1)
        self.assertTrue(owns_case(self.db, "pg-case", owner.token, now=2))
        self.assertFalse(owns_case(self.db, "pg-case", owner.token, now=owner.expires_at))
        with self.assertRaises(CaseNotAvailable):
            preview_deletion(self.db, "pg-case", owner.token, now=owner.expires_at)
        create_case(self.db, case("legacy"))
        self.assertFalse(owns_case(self.db, "legacy", owner.token, now=2))

    def test_stale_deletion_and_unicode_byte_accounting(self):
        owner = create_owned_case(self.db, case(), None)
        preview = preview_deletion(self.db, "pg-case", owner.token)
        append_evidence(self.db, "pg-case", "observation", READING, "reading")
        with self.assertRaises(CaseChanged):
            delete_owned_case(self.db, "pg-case", owner.token, preview.deletion_token)
        with transaction(self.db) as connection:
            expected = connection.execute(
                "SELECT SUM(octet_length(payload)) FROM (SELECT payload FROM apexmedic.cases "
                "UNION ALL SELECT payload FROM apexmedic.case_events) AS all_payloads"
            ).fetchone()[0]
        self.assertEqual(self.totals(), (1, expected))
        preview = preview_deletion(self.db, "pg-case", owner.token)
        delete_owned_case(self.db, "pg-case", owner.token, preview.deletion_token)
        self.assertEqual(self.totals(), (0, 0))

    def test_deletion_failure_rolls_back_every_table(self):
        owner = create_owned_case(self.db, case(), None)
        append_evidence(self.db, "pg-case", "observation", READING, "reading")
        before = self.totals()
        preview = preview_deletion(self.db, "pg-case", owner.token)
        with psycopg.connect(TEST_URL) as connection:
            connection.execute("CREATE TABLE apexmedic.block_delete (case_id text REFERENCES apexmedic.cases(case_id))")
            connection.execute("INSERT INTO apexmedic.block_delete VALUES ('pg-case')")
        with self.assertRaises(StorageUnavailable):
            delete_owned_case(self.db, "pg-case", owner.token, preview.deletion_token)
        self.assertTrue(owns_case(self.db, "pg-case", owner.token))
        self.assertEqual(len(get_history(self.db, "pg-case")), 1)
        self.assertEqual(self.totals(), before)

    def test_rate_budgets_are_shared_and_atomic(self):
        def attempt(_):
            try:
                consume_request(self.db, "same-network", "create", now=100)
                return True
            except RateLimitExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(10))), 5)
        # A fresh handle sees the same counters, rather than resetting them.
        with self.assertRaises(RateLimitExceeded):
            consume_request(PostgresDatabase(TEST_URL), "same-network", "create", now=101)
        consume_request(self.db, "same-network", "create", now=701)

    def test_counter_capacity_is_bounded(self):
        with patch("backend.app.db.rate_limits.MAX_COUNTERS", 1):
            with self.assertRaises(RateLimitExceeded) as caught:
                consume_request(self.db, "one", None, now=100)
        self.assertEqual(caught.exception.status_code, 503)
        with transaction(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM apexmedic.request_counters").fetchone()[0], 0)
