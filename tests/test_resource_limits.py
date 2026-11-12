import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.body_limit import RequestBodyLimit
from backend.app.api.main import create_app
from backend.app.db.ownership import create_owned_case, initialise_ownership
from backend.app.db.repository import (
    append_evidence, create_case, get_case, get_history, initialise_database,
)
from backend.app.limits import (
    MAX_CASE_BYTES, MAX_REQUEST_BYTES, WriteLimitExceeded, check_case_capacity,
    check_new_case,
)
from backend.app.models.case import Case, CompletedTest, Observation


PAYLOAD = {
    "player_description": "Synthetic limit check.",
    "origin": "synthetic",
    "connection_type": "ethernet",
}
READING = {
    "source": "player_report", "context": "Synthetic affected match",
    "latency_ms": 55.0, "packet_loss_pct": 8.0, "fps": 144.0,
}
RESULT = {
    "test_id": "compare_data_centers", "conditions": "Synthetic comparison",
    "outcome": "unchanged", "notes": "No measured cause established.",
}


def sample(case_id="case"):
    return Case(
        case_id=case_id, platform="windows_pc", symptom="unknown", **PAYLOAD,
    )


class BodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def call_body(self, chunks, headers=(), *, max_bytes=8, delay=False):
        sent = []
        consumed = []
        messages = [
            {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
            for i, chunk in enumerate(chunks)
        ]

        async def receive():
            if delay:
                await asyncio.Future()
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def app(scope, incoming, send):
            body = await incoming()
            consumed.append(body["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body["body"]})

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/cases", "headers": list(headers)}
        await RequestBodyLimit(app, max_bytes, 0.01)(scope, receive, send)
        return sent, consumed, messages

    async def test_exact_byte_boundary_replays_all_chunks_once(self):
        sent, consumed, _ = await self.call_body([b"ab", b"cdefgh"])
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(consumed, [b"abcdefgh"])

    async def test_missing_or_understated_length_cannot_bypass_actual_byte_limit(self):
        for headers in ((), ((b"content-length", b"1"),)):
            sent, consumed, _ = await self.call_body([b"1234", b"56789"], headers)
            self.assertEqual(sent[0]["status"], 413)
            self.assertEqual(consumed, [])
            self.assertIn((b"cache-control", b"no-store"), sent[0]["headers"])

    async def test_declared_oversize_is_rejected_without_receiving_body(self):
        sent, consumed, remaining = await self.call_body([b"x"], ((b"content-length", b"9"),))
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(consumed, [])
        self.assertEqual(len(remaining), 1)

    async def test_utf8_is_counted_in_bytes(self):
        sent, consumed, _ = await self.call_body(["தமிழ்".encode("utf-8")])
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(consumed, [])

    async def test_invalid_or_duplicate_content_length_is_rejected(self):
        for headers in (
            ((b"content-length", b"-1"),),
            ((b"content-length", b"invalid"),),
            ((b"content-length", b"9" * 100),),
            ((b"content-length", b"1"), (b"content-length", b"1")),
        ):
            sent, consumed, _ = await self.call_body([b"x"], headers)
            self.assertEqual(sent[0]["status"], 400)
            self.assertEqual(consumed, [])

    async def test_compressed_body_is_not_accepted(self):
        sent, consumed, _ = await self.call_body([b"x"], ((b"content-encoding", b"gzip"),))
        self.assertEqual(sent[0]["status"], 415)
        self.assertEqual(consumed, [])

    async def test_incomplete_body_times_out_before_calling_application(self):
        sent, consumed, _ = await self.call_body([], delay=True)
        self.assertEqual(sent[0]["status"], 408)
        self.assertEqual(consumed, [])

    async def test_disconnected_body_does_not_call_application(self):
        sent, consumed, _ = await self.call_body([])
        self.assertEqual(sent, [])
        self.assertEqual(consumed, [])

    async def test_lifespan_passes_through(self):
        observed = []

        async def app(scope, receive, send):
            observed.append(scope["type"])

        await RequestBodyLimit(app)({"type": "lifespan"}, None, None)
        self.assertEqual(observed, ["lifespan"])


class StorageLimitTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        initialise_database(self.database)
        initialise_ownership(self.database)

    def put_legacy(self, case):
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO cases (case_id, payload) VALUES (?, ?)",
                    (case.case_id, case.model_dump_json()),
                )

    def append(self, kind="observation", entry=None, event_id="event", case_id="case"):
        return append_evidence(
            self.database, case_id, kind,
            json.dumps(READING if entry is None else entry), event_id,
        )

    def test_field_boundaries_accept_exact_lengths_and_reject_one_more(self):
        create_case(self.database, sample())
        fields = (
            ("observation", READING, "context", 2000),
            ("test", RESULT, "test_id", 100),
            ("test", RESULT, "conditions", 2000),
            ("test", RESULT, "notes", 4000),
        )
        for kind, base, field, limit in fields:
            with self.subTest(field=field):
                good = {**base, field: "x" * limit}
                self.assertTrue(self.append(kind, good, field))
                before = get_case(self.database, "case")
                history = get_history(self.database, "case")
                with self.assertRaises(WriteLimitExceeded) as caught:
                    self.append(kind, {**base, field: "x" * (limit + 1)}, field + "-bad")
                self.assertEqual(caught.exception.status_code, 422)
                self.assertEqual(get_case(self.database, "case"), before)
                self.assertEqual(get_history(self.database, "case"), history)

    def test_observation_quota_preserves_retries_and_rejects_changed_payload(self):
        case = sample()
        case.observations = [Observation(**READING) for _ in range(99)]
        create_case(self.database, case)
        self.assertTrue(self.append())
        self.assertFalse(self.append())
        with self.assertRaises(WriteLimitExceeded):
            self.append(event_id="extra")
        with self.assertRaisesRegex(ValueError, "different evidence"):
            self.append(entry={**READING, "fps": 60.0})
        self.assertEqual(len(get_case(self.database, "case").observations), 100)
        self.assertEqual(len(get_history(self.database, "case")), 1)

    def test_test_quota_preserves_retries(self):
        case = sample()
        case.completed_tests = [CompletedTest(**RESULT) for _ in range(99)]
        create_case(self.database, case)
        self.assertTrue(self.append("test", RESULT))
        self.assertFalse(self.append("test", RESULT))
        with self.assertRaises(WriteLimitExceeded):
            self.append("test", RESULT, "extra")
        self.assertEqual(len(get_case(self.database, "case").completed_tests), 100)
        self.assertEqual(len(get_history(self.database, "case")), 1)

    def test_concurrent_last_slot_accepts_only_one_event(self):
        case = sample()
        case.observations = [Observation(**READING) for _ in range(99)]
        create_case(self.database, case)

        def write(event):
            try:
                return self.append(event_id=event)
            except WriteLimitExceeded:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ("first", "second")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(get_case(self.database, "case").observations), 100)
        self.assertEqual(len(get_history(self.database, "case")), 1)

    def test_byte_capacity_exact_boundary_and_utf8(self):
        case = sample()
        payload = case.model_dump_json()
        case.player_description += "x" * (MAX_CASE_BYTES - len(payload.encode("utf-8")))
        payload = case.model_dump_json()
        self.assertEqual(len(payload.encode("utf-8")), MAX_CASE_BYTES)
        check_case_capacity(case, payload)
        case.player_description += "é"
        with self.assertRaises(WriteLimitExceeded):
            check_case_capacity(case, case.model_dump_json())

    def test_oversized_legacy_case_remains_readable_and_new_write_rolls_back(self):
        case = sample()
        case.player_description = "é" * (MAX_CASE_BYTES // 2)
        self.put_legacy(case)
        self.assertEqual(get_case(self.database, "case"), case)
        with self.assertRaises(WriteLimitExceeded):
            self.append()
        self.assertEqual(get_case(self.database, "case"), case)
        self.assertEqual(get_history(self.database, "case"), [])

    def test_legacy_oversized_entry_retry_is_still_a_no_op(self):
        case = sample()
        entry = Observation(**{**READING, "context": "x" * 2001})
        case.observations = [entry for _ in range(101)]
        self.put_legacy(case)
        get_history(self.database, "case")
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO case_events (event_id, case_id, event_type, recorded_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("legacy", "case", "observation", "2026-01-01T00:00:00+00:00", entry.model_dump_json()),
                )
        self.assertFalse(self.append(entry=entry.model_dump(), event_id="legacy"))
        self.assertEqual(get_case(self.database, "case"), case)
        self.assertEqual(len(get_history(self.database, "case")), 1)

    def test_rejected_new_case_creates_no_case_owner_or_session(self):
        case = sample()
        case.player_description = "x" * 4001
        for create in (
            lambda: create_case(self.database, case),
            lambda: create_owned_case(self.database, case, None),
        ):
            with self.assertRaises(WriteLimitExceeded):
                create()
        with closing(sqlite3.connect(self.database)) as connection:
            for table in ("cases", "case_owners", "visitor_sessions"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_import_limits_cover_metadata_and_nested_evidence(self):
        cases = []
        for field, length in (("data_center", 201), ("onset", 501), ("confirmed_cause", 4001)):
            case = sample()
            setattr(case, field, "x" * length)
            cases.append(case)
        case = sample()
        case.observations = [Observation(**{**READING, "context": "x" * 2001})]
        cases.append(case)
        case = sample()
        case.completed_tests = [CompletedTest(**RESULT) for _ in range(101)]
        cases.append(case)
        for case in cases:
            with self.assertRaises(WriteLimitExceeded):
                check_new_case(case, case.model_dump_json())


class ResourceLimitApiTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        self.client = self.enterContext(TestClient(create_app(self.database)))

    def create(self):
        response = self.client.post("/cases", json=PAYLOAD)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case_id"]

    def test_oversized_request_is_rejected_before_creating_anything(self):
        body = json.dumps({**PAYLOAD, "player_description": "x" * MAX_REQUEST_BYTES})
        response = self.client.post("/cases", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visitor_sessions").fetchone()[0], 0)

    def test_long_entry_returns_422_without_changing_history(self):
        case_id = self.create()
        before = self.client.get(f"/cases/{case_id}").json()
        for endpoint, entry in (
            ("observations", {**READING, "context": "x" * 2001}),
            ("tests", {**RESULT, "notes": "x" * 4001}),
        ):
            response = self.client.post(
                f"/cases/{case_id}/{endpoint}",
                json={"event_id": endpoint, "entry": entry},
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("at most", response.json()["detail"])
        self.assertEqual(self.client.get(f"/cases/{case_id}").json(), before)
        self.assertEqual(self.client.get(f"/cases/{case_id}/history").json(), [])

    def test_full_case_returns_409_and_retry_still_succeeds(self):
        case_id = self.create()
        case = get_case(self.database, case_id)
        case.observations = [Observation(**READING) for _ in range(99)]
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("UPDATE cases SET payload = ? WHERE case_id = ?", (case.model_dump_json(), case_id))
        url = f"/cases/{case_id}/observations"
        payload = {"event_id": "last", "entry": READING}
        self.assertTrue(self.client.post(url, json=payload).json()["added"])
        response = self.client.post(url, json={**payload, "event_id": "extra"})
        self.assertEqual(response.status_code, 409, response.text)
        retry = self.client.post(url, json=payload)
        self.assertEqual(retry.status_code, 200)
        self.assertFalse(retry.json()["added"])
        self.assertEqual(len(self.client.get(f"/cases/{case_id}/history").json()), 1)

    def test_existing_large_case_can_be_read_over_http(self):
        case_id = self.create()
        case = get_case(self.database, case_id)
        case.player_description = "x" * 4001
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("UPDATE cases SET payload = ? WHERE case_id = ?", (case.model_dump_json(), case_id))
        response = self.client.get(f"/cases/{case_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["player_description"], case.player_description)

    def test_invalid_numeric_readings_still_fail_before_storage(self):
        case_id = self.create()
        response = self.client.post(
            f"/cases/{case_id}/observations",
            json={"event_id": "invalid", "entry": {**READING, "packet_loss_pct": 101}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get(f"/cases/{case_id}/history").json(), [])


if __name__ == "__main__":
    unittest.main()
