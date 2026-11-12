import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.db import quotas
from backend.app.db.case_deletion import (
    CaseChanged, CaseNotAvailable, delete_owned_case, preview_deletion,
)
from backend.app.db.ownership import create_owned_case, initialise_ownership, owns_case
from backend.app.db.repository import (
    append_evidence, create_case, get_case, get_history, initialise_database,
)
from backend.app.llm import ModelExplanation
from backend.app.models.case import Case


PAYLOAD = {
    "player_description": "Synthetic case for deletion checks.",
    "origin": "synthetic", "connection_type": "ethernet",
}
READING = {"source": "player_report", "context": "Synthetic match", "packet_loss_pct": 8.0}
RESULT = {"test_id": "compare_data_centers", "conditions": "Synthetic", "outcome": "unchanged", "notes": "Synthetic result"}
PROVIDER = "backend.app.api.explanations.generate_explanation"


def sample(name):
    return Case(case_id=name, platform="windows_pc", symptom="unknown", **PAYLOAD)


class CaseDeletionStorageTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        initialise_database(self.database)
        initialise_ownership(self.database)
        self.owner = create_owned_case(self.database, sample("selected"), None)

    def preview(self, **kwargs):
        return preview_deletion(self.database, "selected", self.owner.token, **kwargs)

    def evidence(self, event="reading"):
        return append_evidence(self.database, "selected", "observation", json.dumps(READING), event)

    def database_state(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return {
                name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                for name in ("cases", "case_events", "case_owners", "visitor_sessions", "storage_totals")
            }

    def test_preview_is_read_only_and_matches_evidence_counts(self):
        self.evidence()
        before = self.database_state()
        first, second = self.preview(), self.preview()
        self.assertEqual(first, second)
        self.assertEqual(first.observation_count, 1)
        self.assertEqual(first.test_result_count, 0)
        self.assertEqual(first.history_event_count, 1)
        self.assertEqual(len(first.deletion_token), 64)
        self.assertEqual(self.database_state(), before)

    def test_deletion_removes_all_related_records_and_frees_totals(self):
        self.evidence()
        token = self.preview().deletion_token
        delete_owned_case(self.database, "selected", self.owner.token, token)
        self.assertIsNone(get_case(self.database, "selected"))
        state = self.database_state()
        for name in ("cases", "case_events", "case_owners", "visitor_sessions"):
            self.assertEqual(state[name], [])
        self.assertEqual(state["storage_totals"], [(1, 0, 0)])

    def test_other_cases_and_their_ownership_are_preserved(self):
        create_owned_case(self.database, sample("same-owner"), self.owner.token)
        other = create_owned_case(self.database, sample("other-owner"), None)
        self.evidence()
        delete_owned_case(self.database, "selected", self.owner.token, self.preview().deletion_token)
        self.assertTrue(owns_case(self.database, "same-owner", self.owner.token))
        self.assertTrue(owns_case(self.database, "other-owner", other.token))
        self.assertEqual(get_case(self.database, "same-owner"), sample("same-owner"))
        self.assertEqual(get_case(self.database, "other-owner"), sample("other-owner"))
        with closing(sqlite3.connect(self.database)) as connection:
            counted = connection.execute("SELECT case_count, payload_bytes FROM storage_totals").fetchone()
            actual = connection.execute("SELECT COUNT(*), SUM(length(CAST(payload AS BLOB))) FROM cases").fetchone()
        self.assertEqual(counted, actual)

    def test_missing_forged_foreign_and_expired_credentials_cannot_delete(self):
        token = self.preview().deletion_token
        other = create_owned_case(self.database, sample("other"), None)
        before = self.database_state()
        for cookie, now in ((None, None), ("forged", None), ("A" * 43, None), (other.token, None), (self.owner.token, self.owner.expires_at)):
            with self.subTest(cookie=cookie is None, now=now):
                with self.assertRaises(CaseNotAvailable):
                    delete_owned_case(self.database, "selected", cookie, token, now=now)
                with self.assertRaises(CaseNotAvailable):
                    preview_deletion(self.database, "selected", cookie, now=now)
        self.assertEqual(self.database_state(), before)

    def test_legacy_unowned_case_cannot_be_deleted_by_a_browser(self):
        create_case(self.database, sample("legacy"))
        with self.assertRaises(CaseNotAvailable):
            delete_owned_case(self.database, "legacy", self.owner.token, "0" * 64)
        self.assertIsNotNone(get_case(self.database, "legacy"))

    def test_new_observation_requires_another_review(self):
        token = self.preview().deletion_token
        self.evidence()
        before = self.database_state()
        with self.assertRaises(CaseChanged):
            delete_owned_case(self.database, "selected", self.owner.token, token)
        self.assertEqual(self.database_state(), before)
        self.assertNotEqual(self.preview().deletion_token, token)

    def test_answer_and_test_result_change_the_snapshot(self):
        for kind, entry in (
            ("answer", {"question_id": "clarify_symptom", "symptom": "packet_loss"}),
            ("test", RESULT),
        ):
            token = self.preview().deletion_token
            append_evidence(self.database, "selected", kind, json.dumps(entry), kind)
            with self.assertRaises(CaseChanged):
                delete_owned_case(self.database, "selected", self.owner.token, token)

    def test_history_only_changes_are_detected(self):
        self.evidence()
        token = self.preview().deletion_token
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("UPDATE case_events SET recorded_at = ?", ("2026-01-01T00:00:00+00:00",))
        with self.assertRaises(CaseChanged):
            delete_owned_case(self.database, "selected", self.owner.token, token)

    def test_identical_evidence_retry_does_not_invalidate_preview(self):
        self.evidence()
        preview = self.preview()
        self.assertFalse(self.evidence())
        self.assertEqual(self.preview(), preview)
        delete_owned_case(self.database, "selected", self.owner.token, preview.deletion_token)

    def test_failure_after_history_delete_rolls_back_all_rows_and_totals(self):
        self.evidence()
        token = self.preview().deletion_token
        before = self.database_state()
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    "CREATE TRIGGER reject_case_delete BEFORE DELETE ON cases "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            delete_owned_case(self.database, "selected", self.owner.token, token)
        self.assertEqual(self.database_state(), before)

    def test_repeated_delete_returns_not_found_without_negative_accounting(self):
        token = self.preview().deletion_token
        delete_owned_case(self.database, "selected", self.owner.token, token)
        before = self.database_state()
        with self.assertRaises(CaseNotAvailable):
            delete_owned_case(self.database, "selected", self.owner.token, token)
        self.assertEqual(self.database_state(), before)

    def test_concurrent_deletes_have_one_success(self):
        token = self.preview().deletion_token
        barrier = Barrier(2)

        def remove(_):
            barrier.wait(timeout=5)
            try:
                delete_owned_case(self.database, "selected", self.owner.token, token)
                return True
            except CaseNotAvailable:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(remove, (1, 2))), [False, True])
        self.assertEqual(self.database_state()["storage_totals"], [(1, 0, 0)])

    def test_concurrent_append_is_preserved_or_rejected_without_resurrection(self):
        token = self.preview().deletion_token
        barrier = Barrier(2)

        def append():
            barrier.wait(timeout=5)
            try:
                self.evidence()
                return "saved"
            except ValueError:
                return "missing"

        def remove():
            barrier.wait(timeout=5)
            try:
                delete_owned_case(self.database, "selected", self.owner.token, token)
                return "deleted"
            except CaseChanged:
                return "changed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            added, deleted = pool.submit(append), pool.submit(remove)
            outcome = added.result(timeout=10), deleted.result(timeout=10)
        self.assertIn(outcome, (("saved", "changed"), ("missing", "deleted")))
        if outcome[0] == "saved":
            self.assertEqual(len(get_case(self.database, "selected").observations), 1)
        else:
            self.assertIsNone(get_case(self.database, "selected"))
            self.assertEqual(self.database_state()["case_events"], [])

    def test_concurrent_case_creation_keeps_access_to_the_new_case(self):
        token = self.preview().deletion_token
        barrier = Barrier(2)

        def create():
            barrier.wait(timeout=5)
            return create_owned_case(self.database, sample("new"), self.owner.token)

        def remove():
            barrier.wait(timeout=5)
            delete_owned_case(self.database, "selected", self.owner.token, token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            created, deleted = pool.submit(create), pool.submit(remove)
            owner = created.result(timeout=10)
            deleted.result(timeout=10)
        self.assertTrue(owns_case(self.database, "new", owner.token))
        self.assertIsNone(get_case(self.database, "selected"))

    def test_deletion_works_at_capacity_and_frees_a_session_slot(self):
        token = self.preview().deletion_token
        with patch.object(quotas, "MAX_CASES", 1), patch.object(quotas, "MAX_SESSIONS", 1), patch.object(quotas, "MAX_PAYLOAD_BYTES", 0):
            delete_owned_case(self.database, "selected", self.owner.token, token)
        with patch.object(quotas, "MAX_CASES", 1), patch.object(quotas, "MAX_SESSIONS", 1):
            new = create_owned_case(self.database, sample("new"), self.owner.token)
        self.assertNotEqual(new.token, self.owner.token)
        self.assertTrue(owns_case(self.database, "new", new.token))


class CaseDeletionApiTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        self.app = create_app(self.database)
        self.client = self.enterContext(TestClient(self.app))
        self.other = self.enterContext(TestClient(self.app))
        response = self.client.post("/cases", json=PAYLOAD)
        self.assertEqual(response.status_code, 201)
        self.case_id = response.json()["case_id"]
        self.url = "/cases/" + self.case_id

    def deletion_body(self):
        response = self.client.get(self.url + "/deletion-preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return {"confirm_case_id": self.case_id, "deletion_token": response.json()["deletion_token"]}

    def test_owner_deletes_case_and_all_endpoints_stop_serving_it(self):
        self.client.post(self.url + "/observations", json={"event_id": "entry", "entry": READING})
        response = self.client.request("DELETE", self.url, json=self.deletion_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"case_id": self.case_id, "deleted": True})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        for suffix in ("", "/history", "/next-step", "/deletion-preview"):
            self.assertEqual(self.client.get(self.url + suffix).status_code, 404)
        with patch(PROVIDER, new_callable=AsyncMock) as provider:
            response = self.client.post(self.url + "/explanation", json={"decision_token": "0" * 64})
            self.assertEqual(response.status_code, 404)
            provider.assert_not_awaited()

    def test_foreign_browser_gets_same_404_as_missing_case(self):
        body = self.deletion_body()
        self.other.post("/cases", json=PAYLOAD)
        before = get_case(self.database, self.case_id)
        for url in (self.url, "/cases/not-present"):
            self.assertEqual(self.other.get(url + "/deletion-preview").json(), {"detail": "Case not found."})
            response = self.other.request("DELETE", url, json=body)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": "Case not found."})
        self.assertEqual(get_case(self.database, self.case_id), before)

    def test_full_id_and_valid_snapshot_are_required(self):
        body = self.deletion_body()
        for changes, expected in (
            ({"confirm_case_id": "another-case"}, 422),
            ({"deletion_token": "invalid"}, 422),
            ({"deletion_token": "0" * 64}, 409),
        ):
            response = self.client.request("DELETE", self.url, json={**body, **changes})
            self.assertEqual(response.status_code, expected, response.text)
        self.assertIsNotNone(get_case(self.database, self.case_id))

    def test_new_evidence_requires_fresh_confirmation(self):
        old = self.deletion_body()
        self.client.post(self.url + "/observations", json={"event_id": "new", "entry": READING})
        response = self.client.request("DELETE", self.url, json=old)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(get_history(self.database, self.case_id)), 1)
        self.assertEqual(self.client.request("DELETE", self.url, json=self.deletion_body()).status_code, 200)

    def test_cross_origin_and_simple_content_deletion_are_blocked(self):
        body = self.deletion_body()
        for headers in ({"Origin": "https://other.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            response = self.client.request("DELETE", self.url, json=body, headers=headers)
            self.assertEqual(response.status_code, 403)
        response = self.client.request("DELETE", self.url, content=json.dumps(body), headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        self.assertIsNotNone(get_case(self.database, self.case_id))

    def test_preview_is_available_on_management_page(self):
        response = self.client.get("/ui/manage.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="delete-button"', response.text)
        self.assertIn("Existing backup copies are not removed", response.text)
        self.assertEqual(self.client.get("/ui/manage.js").status_code, 200)

    def test_storage_busy_returns_503_without_deletion(self):
        body = self.deletion_body()
        with patch("backend.app.api.deletion.delete_owned_case", side_effect=sqlite3.OperationalError("busy")):
            response = self.client.request("DELETE", self.url, json=body)
        self.assertEqual(response.status_code, 503)
        self.assertIsNotNone(get_case(self.database, self.case_id))

    def test_deletion_during_generation_discards_explanation_and_releases_lock(self):
        entered = Event()
        proceed = Event()

        async def slow_provider(_):
            entered.set()
            await asyncio.to_thread(proceed.wait, 5)
            return ModelExplanation(explanation="Please clarify the symptom.")

        decision = self.client.get(self.url + "/next-step").json()
        body = self.deletion_body()
        with patch(PROVIDER, side_effect=slow_provider):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.client.post, self.url + "/explanation", json={"decision_token": decision["decision_token"]})
                try:
                    self.assertTrue(entered.wait(3))
                    response = self.client.request("DELETE", self.url, json=body)
                    self.assertEqual(response.status_code, 200)
                finally:
                    proceed.set()
                self.assertEqual(pending.result(timeout=8).status_code, 404)
        new_id = self.client.post("/cases", json=PAYLOAD).json()["case_id"]
        token = self.client.get(f"/cases/{new_id}/next-step").json()["decision_token"]
        with patch(PROVIDER, new_callable=AsyncMock) as provider:
            provider.return_value = ModelExplanation(explanation="Please clarify the symptom.")
            self.assertEqual(self.client.post(f"/cases/{new_id}/explanation", json={"decision_token": token}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
