import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.db import quotas, rate_limits as rates
from backend.app.db.ownership import create_owned_case, initialise_ownership, owns_case
from backend.app.db.repository import (
    append_evidence, create_case, get_case, get_history, initialise_database,
)
from backend.app.limits import WriteLimitExceeded
from backend.app.llm import ModelExplanation
from backend.app.models.case import Case, Observation


PAYLOAD = {
    "player_description": "Synthetic service quota check.",
    "origin": "synthetic", "connection_type": "ethernet",
}
READING = {"source": "player_report", "context": "Synthetic match", "packet_loss_pct": 8.0}


def sample(name):
    return Case(case_id=name, platform="windows_pc", symptom="unknown", **PAYLOAD)


class PersistentRateTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "rates.sqlite3"
        rates.initialise_rate_limits(self.database)

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT bucket, hits, expires_at FROM request_counters ORDER BY bucket").fetchall()

    def test_creation_limit_and_retry_after(self):
        for _ in range(5):
            rates.consume_request(self.database, "peer", "create", now=100)
        before = self.rows()
        with self.assertRaises(rates.RateLimitExceeded) as caught:
            rates.consume_request(self.database, "peer", "create", now=101)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 599)
        self.assertEqual(self.rows(), before)

    def test_counter_survives_reinitialisation(self):
        for _ in range(5):
            rates.consume_request(self.database, "peer", "create", now=100)
        rates.initialise_rate_limits(self.database)
        with self.assertRaises(rates.RateLimitExceeded):
            rates.consume_request(self.database, "peer", "create", now=101)

    def test_expiry_allows_request_at_exact_boundary(self):
        for _ in range(5):
            rates.consume_request(self.database, "peer", "create", now=100)
        with self.assertRaises(rates.RateLimitExceeded):
            rates.consume_request(self.database, "peer", "create", now=699.9)
        rates.consume_request(self.database, "peer", "create", now=700)
        row = next(row for row in self.rows() if row[0] == "network-create:peer")
        self.assertEqual(row[1:], (1, 1300))

    def test_global_budget_cannot_be_reset_with_another_peer(self):
        with patch.object(rates, "GENERAL", (rates.Budget("global", 2, 60),)):
            rates.consume_request(self.database, "a", None, now=100)
            rates.consume_request(self.database, "b", None, now=100)
            with self.assertRaises(rates.RateLimitExceeded):
                rates.consume_request(self.database, "c", None, now=101)

    def test_concurrent_requests_cannot_both_take_final_slot(self):
        with patch.object(rates, "GENERAL", (rates.Budget("global", 1, 60),)):
            def consume(peer):
                try:
                    rates.consume_request(self.database, peer, None, now=100)
                    return True
                except rates.RateLimitExceeded:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(consume, ("a", "b")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.rows()[0][1], 1)

    def test_counter_table_is_bounded_and_recovers_after_expiry(self):
        with patch.object(rates, "MAX_COUNTERS", 2):
            rates.consume_request(self.database, "a", None, now=100)
            with self.assertRaises(rates.RateLimitExceeded) as caught:
                rates.consume_request(self.database, "b", None, now=101)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(len(self.rows()), 2)
            rates.consume_request(self.database, "b", None, now=160)
            self.assertEqual(len(self.rows()), 2)

    def test_ipv6_rotation_and_mapped_ipv4_share_network_buckets(self):
        self.assertEqual(rates.network_key("2001:db8:1:2::1"), rates.network_key("2001:db8:1:2::ffff"))
        self.assertNotEqual(rates.network_key("2001:db8:1:2::1"), rates.network_key("2001:db8:1:3::1"))
        self.assertEqual(rates.network_key("::ffff:192.0.2.1"), rates.network_key("192.0.2.1"))
        self.assertEqual(rates.network_key(None), rates.network_key("invalid-peer"))

    def test_categories_have_separate_budgets(self):
        for _ in range(6):
            rates.consume_request(self.database, "a", "explanation", now=100)
        with self.assertRaises(rates.RateLimitExceeded):
            rates.consume_request(self.database, "a", "explanation", now=100)
        rates.consume_request(self.database, "a", "evidence", now=100)
        rates.consume_request(self.database, "a", None, now=100)


class StorageQuotaTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        initialise_database(self.database)
        initialise_ownership(self.database)

    def actual_and_counted(self):
        with closing(sqlite3.connect(self.database)) as connection:
            actual = connection.execute(
                "SELECT (SELECT COUNT(*) FROM cases), "
                "COALESCE((SELECT SUM(length(CAST(payload AS BLOB))) FROM cases), 0) + "
                "COALESCE((SELECT SUM(length(CAST(payload AS BLOB))) FROM case_events), 0)"
            ).fetchone()
            counted = connection.execute("SELECT case_count, payload_bytes FROM storage_totals WHERE id = 1").fetchone()
        return actual, counted

    def test_accounting_includes_utf8_case_and_history_payloads(self):
        case = sample("unicode")
        case.player_description = "தமிழ் synthetic case"
        create_case(self.database, case)
        append_evidence(self.database, "unicode", "observation", json.dumps(READING), "event")
        actual, counted = self.actual_and_counted()
        self.assertEqual(actual, counted)
        self.assertGreater(counted[1], len(get_case(self.database, "unicode").model_dump_json().encode("utf-8")))

    def test_case_quota_rejects_browser_and_cli_creation_without_orphan_session(self):
        create_case(self.database, sample("first"))
        with patch.object(quotas, "MAX_CASES", 1):
            for create in (
                lambda: create_case(self.database, sample("cli")),
                lambda: create_owned_case(self.database, sample("web"), None),
            ):
                with self.assertRaises(WriteLimitExceeded) as caught:
                    create()
                self.assertEqual(caught.exception.status_code, 503)
        self.assertIsNotNone(get_case(self.database, "first"))
        self.assertIsNone(get_case(self.database, "cli"))
        self.assertIsNone(get_case(self.database, "web"))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visitor_sessions").fetchone()[0], 0)

    def test_case_capacity_does_not_block_evidence_for_existing_case(self):
        create_case(self.database, sample("first"))
        with patch.object(quotas, "MAX_CASES", 1):
            self.assertTrue(append_evidence(self.database, "first", "observation", json.dumps(READING), "e"))

    def test_session_case_cap_preserves_cookie_access(self):
        owner = create_owned_case(self.database, sample("first"), None)
        with patch.object(quotas, "MAX_CASES_PER_SESSION", 1):
            with self.assertRaises(WriteLimitExceeded) as caught:
                create_owned_case(self.database, sample("second"), owner.token)
            self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(owns_case(self.database, "first", owner.token))
        self.assertIsNone(get_case(self.database, "second"))

    def test_session_cap_blocks_new_session_but_allows_existing_session(self):
        owner = create_owned_case(self.database, sample("first"), None)
        with patch.object(quotas, "MAX_SESSIONS", 1):
            with self.assertRaises(WriteLimitExceeded):
                create_owned_case(self.database, sample("stranger"), None)
            create_owned_case(self.database, sample("second"), owner.token)
        self.assertTrue(owns_case(self.database, "second", owner.token))

    def test_payload_quota_rejects_whole_evidence_transaction_and_keeps_retry_safe(self):
        create_case(self.database, sample("first"))
        append_evidence(self.database, "first", "observation", json.dumps(READING), "original")
        before = get_case(self.database, "first")
        history = get_history(self.database, "first")
        _, counted = self.actual_and_counted()
        with patch.object(quotas, "MAX_PAYLOAD_BYTES", counted[1]):
            self.assertFalse(append_evidence(self.database, "first", "observation", json.dumps(READING), "original"))
            with self.assertRaises(WriteLimitExceeded):
                append_evidence(self.database, "first", "observation", json.dumps(READING), "new")
        self.assertEqual(get_case(self.database, "first"), before)
        self.assertEqual(get_history(self.database, "first"), history)
        self.assertEqual(self.actual_and_counted(), (counted, counted))

    def test_history_failure_rolls_back_accounting_and_case(self):
        create_case(self.database, sample("first"))
        before = self.actual_and_counted()
        case = get_case(self.database, "first")
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    "CREATE TRIGGER fail_event BEFORE INSERT ON case_events "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            append_evidence(self.database, "first", "observation", json.dumps(READING), "failure")
        self.assertEqual(self.actual_and_counted(), before)
        self.assertEqual(get_case(self.database, "first"), case)

    def test_concurrent_creations_cannot_both_take_final_case_slot(self):
        with patch.object(quotas, "MAX_CASES", 1):
            def create(name):
                try:
                    create_owned_case(self.database, sample(name), None)
                    return True
                except WriteLimitExceeded:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(create, ("one", "two")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.actual_and_counted()[1][0], 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visitor_sessions").fetchone()[0], 1)

    def test_startup_backfills_legacy_usage_without_rewriting_payloads(self):
        create_case(self.database, sample("old"))
        append_evidence(self.database, "old", "observation", json.dumps(READING), "event")
        before = get_case(self.database, "old")
        history = get_history(self.database, "old")
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("UPDATE storage_totals SET case_count = 0, payload_bytes = 0")
        with patch.object(quotas, "MAX_CASES", 0), patch.object(quotas, "MAX_PAYLOAD_BYTES", 0):
            initialise_database(self.database)
            initialise_database(self.database)
        self.assertEqual(get_case(self.database, "old"), before)
        self.assertEqual(get_history(self.database, "old"), history)
        actual, counted = self.actual_and_counted()
        self.assertEqual(actual, counted)

    def test_byte_budget_accepts_exact_boundary_for_new_case(self):
        case = sample("boundary")
        size = len(case.model_dump_json().encode("utf-8"))
        with patch.object(quotas, "MAX_PAYLOAD_BYTES", size):
            create_case(self.database, case)
            with self.assertRaises(WriteLimitExceeded):
                create_case(self.database, sample("next"))
        self.assertEqual(self.actual_and_counted(), ((1, size), (1, size)))

    def test_concurrent_evidence_cannot_both_take_remaining_byte_budget(self):
        case = sample("first")
        create_case(self.database, case)
        entry = Observation(**READING)
        updated = case.model_copy(deep=True)
        updated.observations.append(entry)
        capacity = (
            len(updated.model_dump_json().encode("utf-8"))
            + len(entry.model_dump_json().encode("utf-8"))
        )

        def write(event):
            try:
                return append_evidence(self.database, "first", "observation", json.dumps(READING), event)
            except WriteLimitExceeded:
                return False

        with patch.object(quotas, "MAX_PAYLOAD_BYTES", capacity):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(write, ("one", "two")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(get_history(self.database, "first")), 1)
        self.assertEqual(self.actual_and_counted(), ((1, capacity), (1, capacity)))

    def test_migration_from_database_without_accounting_preserves_data(self):
        create_case(self.database, sample("legacy"))
        append_evidence(self.database, "legacy", "observation", json.dumps(READING), "old")
        before = get_case(self.database, "legacy")
        history = get_history(self.database, "legacy")
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                for table in ("cases", "case_events"):
                    for event in ("insert", "update", "delete"):
                        connection.execute(f"DROP TRIGGER budget_{table}_{event}")
                connection.execute("DROP TABLE storage_totals")
        initialise_database(self.database)
        self.assertEqual(get_case(self.database, "legacy"), before)
        self.assertEqual(get_history(self.database, "legacy"), history)
        actual, counted = self.actual_and_counted()
        self.assertEqual(actual, counted)
        append_evidence(self.database, "legacy", "observation", json.dumps(READING), "new")
        actual, counted = self.actual_and_counted()
        self.assertEqual(actual, counted)


class QuotaApiTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        self.client = self.enterContext(TestClient(create_app(self.database)))

    def create(self):
        response = self.client.post("/cases", json=PAYLOAD)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case_id"]

    def test_new_cookies_and_forged_forwarding_headers_do_not_reset_limit(self):
        ids = []
        for i in range(5):
            self.client.cookies.clear()
            response = self.client.post("/cases", json=PAYLOAD, headers={"X-Forwarded-For": f"192.0.2.{i}"})
            self.assertEqual(response.status_code, 201)
            ids.append(response.json()["case_id"])
        response = self.client.post("/cases/", json=PAYLOAD, headers={"Forwarded": "for=198.51.100.1"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.get("/cases/" + ids[-1]).status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_restart_does_not_reset_rate_budget(self):
        for _ in range(5):
            self.create()
        with TestClient(create_app(self.database), cookies=dict(self.client.cookies)) as restarted:
            self.assertEqual(restarted.post("/cases", json=PAYLOAD).status_code, 429)

    def test_rate_rejection_happens_before_bad_body_is_parsed(self):
        for _ in range(5):
            self.create()
        response = self.client.post("/cases", content=b"broken-json", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 429)

    def test_limited_explanation_does_not_call_provider(self):
        case_id = self.create()
        url = f"/cases/{case_id}"
        token = self.client.get(url + "/next-step").json()["decision_token"]
        with patch("backend.app.api.explanations.generate_explanation", new_callable=AsyncMock) as provider:
            provider.return_value = ModelExplanation(explanation="Please clarify the symptom.")
            for _ in range(6):
                response = self.client.post(url + "/explanation", json={"decision_token": token})
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(provider.await_count, 6)
            self.assertEqual(self.client.post(url + "/explanation", json={"decision_token": token}).status_code, 429)
            self.assertEqual(provider.await_count, 6)
        self.assertEqual(self.client.get(url + "/history").json(), [])

    def test_unavailable_counter_store_fails_closed(self):
        with patch("backend.app.api.rate_limit.consume_request", side_effect=sqlite3.OperationalError("locked")):
            response = self.client.post("/cases", json=PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "5")
        self.assertNotIn("set-cookie", response.headers)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 0)

    def test_storage_quota_returns_clear_503_without_new_session(self):
        case_id = self.create()
        self.client.cookies.clear()
        with patch.object(quotas, "MAX_CASES", 1):
            response = self.client.post("/cases", json=PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("retry-after", response.headers)
        self.assertNotIn("set-cookie", response.headers)
        self.assertIsNotNone(get_case(self.database, case_id))

    def test_evidence_retry_is_safe_after_rate_window_recovers(self):
        case_id = self.create()
        url = f"/cases/{case_id}/observations"
        payload = {"event_id": "repeat", "entry": READING}
        self.assertTrue(self.client.post(url, json=payload).json()["added"])
        with patch("backend.app.api.rate_limit.consume_request", side_effect=rates.RateLimitExceeded(1)):
            self.assertEqual(self.client.post(url, json=payload).status_code, 429)
        response = self.client.post(url, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["added"])
        self.assertEqual(len(get_history(self.database, case_id)), 1)


if __name__ == "__main__":
    unittest.main()
