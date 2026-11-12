import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app.api.access import COOKIE_NAME, DEFAULT_ORIGIN, validate_public_origin
from backend.app.api.main import create_app
from backend.app.db.ownership import (
    SESSION_SECONDS, create_owned_case, initialise_ownership, owns_case,
)
from backend.app.db.repository import (
    append_evidence, create_case, get_case, get_history, initialise_database,
)
from backend.app.llm import ModelExplanation
from backend.app.models.case import Case


PROVIDER = "backend.app.api.explanations.generate_explanation"
PAYLOAD = {
    "player_description": "Synthetic ownership test.",
    "origin": "synthetic",
    "connection_type": "ethernet",
}
READING = {
    "source": "player_report", "context": "Synthetic match",
    "latency_ms": 55.0, "packet_loss_pct": 8.0, "fps": 144.0,
}


def sample(case_id):
    return Case(
        case_id=case_id, platform="windows_pc", symptom="unknown", **PAYLOAD,
    )


class OwnershipStorageTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        initialise_database(self.database)
        initialise_ownership(self.database)

    def test_migration_preserves_existing_cases_and_history(self):
        create_case(self.database, sample("legacy"))
        append_evidence(self.database, "legacy", "observation", json.dumps(READING), "old-event")
        before = get_case(self.database, "legacy").model_dump()
        history = get_history(self.database, "legacy")
        initialise_ownership(self.database)
        initialise_ownership(self.database)
        self.assertEqual(get_case(self.database, "legacy").model_dump(), before)
        self.assertEqual(get_history(self.database, "legacy"), history)
        new = create_owned_case(self.database, sample("new"), None)
        self.assertFalse(owns_case(self.database, "legacy", new.token))

    def test_cookie_is_hashed_and_reused_without_extending_expiry(self):
        first = create_owned_case(self.database, sample("first"), None, now=100)
        second = create_owned_case(self.database, sample("second"), first.token, now=200)
        self.assertEqual(first, second)
        self.assertEqual(first.expires_at, 100 + SESSION_SECONDS)
        with closing(sqlite3.connect(self.database)) as connection:
            hashes = connection.execute("SELECT token_hash FROM visitor_sessions").fetchall()
        self.assertEqual(len(hashes), 1)
        self.assertEqual(len(hashes[0][0]), 64)
        self.assertNotEqual(hashes[0][0], first.token)
        self.assertTrue(owns_case(self.database, "first", first.token, now=200))
        self.assertTrue(owns_case(self.database, "second", first.token, now=200))

    def test_forged_and_expired_tokens_cannot_claim_an_existing_case(self):
        first = create_owned_case(self.database, sample("first"), "A" * 43, now=100)
        self.assertNotEqual(first.token, "A" * 43)
        self.assertFalse(owns_case(self.database, "first", first.token, now=first.expires_at))
        second = create_owned_case(
            self.database, sample("second"), first.token, now=first.expires_at,
        )
        self.assertNotEqual(second.token, first.token)
        self.assertFalse(owns_case(self.database, "first", second.token, now=first.expires_at))

    def test_owner_insert_failure_rolls_back_session_and_case(self):
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    "CREATE TRIGGER fail_owner BEFORE INSERT ON case_owners "
                    "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            create_owned_case(self.database, sample("rollback"), None)
        self.assertIsNone(get_case(self.database, "rollback"))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visitor_sessions").fetchone()[0], 0)

    def test_concurrent_creations_keep_the_same_owner(self):
        owner = create_owned_case(self.database, sample("first"), None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            sessions = list(pool.map(
                lambda name: create_owned_case(self.database, sample(name), owner.token),
                ("second", "third"),
            ))
        self.assertTrue(all(session.token == owner.token for session in sessions))
        for name in ("first", "second", "third"):
            self.assertTrue(owns_case(self.database, name, owner.token))


class CaseOwnershipApiTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(folder) / "cases.sqlite3"
        app = create_app(self.database)
        self.owner = self.enterContext(TestClient(app))
        self.other = self.enterContext(TestClient(app))

    def create(self, client, **changes):
        response = client.post("/cases", json={**PAYLOAD, **changes})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        return response

    def test_owner_can_use_every_case_endpoint_and_retry_evidence(self):
        case_id = self.create(self.owner).json()["case_id"]
        url = f"/cases/{case_id}"
        read = self.owner.get(url)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.headers.get("cache-control"), "no-store")
        decision = self.owner.get(url + "/next-step").json()
        with patch(PROVIDER, new_callable=AsyncMock) as provider:
            provider.return_value = ModelExplanation(explanation="Clarify the symptom.")
            response = self.owner.post(url + "/explanation", json={"decision_token": decision["decision_token"]})
        self.assertEqual(response.status_code, 200, response.text)
        provider.assert_awaited_once()
        events = (
            ("answers", {"question_id": "clarify_symptom", "symptom": "packet_loss"}),
            ("observations", READING),
            ("tests", {"test_id": "compare_data_centers", "conditions": "Synthetic", "outcome": "unchanged", "notes": "Synthetic result"}),
        )
        for route, entry in events:
            payload = {"event_id": f"owner-{route}", "entry": entry}
            response = self.owner.post(f"{url}/{route}", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["added"])
            retry = self.owner.post(f"{url}/{route}", json=payload)
            self.assertFalse(retry.json()["added"])
        self.assertEqual(len(self.owner.get(url + "/history").json()), 3)

    def test_other_visitor_cannot_read_write_or_generate_for_owner(self):
        case_id = self.create(self.owner).json()["case_id"]
        self.create(self.other)
        url = f"/cases/{case_id}"
        before = self.owner.get(url).json()
        before_history = self.owner.get(url + "/history").json()
        decision = self.owner.get(url + "/next-step").json()
        with patch(PROVIDER, new_callable=AsyncMock) as provider:
            for suffix in ("", "/next-step", "/history"):
                self.assertEqual(self.other.get(url + suffix).status_code, 404)
            payloads = {
                "answers": {"event_id": "foreign-answer", "entry": {"question_id": "clarify_symptom", "symptom": "packet_loss"}},
                "observations": {"event_id": "foreign-reading", "entry": READING},
                "tests": {"event_id": "foreign-test", "entry": {"test_id": "compare_data_centers", "conditions": "Synthetic", "outcome": "improved", "notes": "Synthetic"}},
                "explanation": {"decision_token": decision["decision_token"]},
            }
            for suffix, payload in payloads.items():
                with self.subTest(endpoint=suffix):
                    response = self.other.post(f"{url}/{suffix}", json=payload)
                    self.assertEqual(response.status_code, 404, response.text)
            provider.assert_not_awaited()
        self.assertEqual(self.owner.get(url).json(), before)
        self.assertEqual(self.owner.get(url + "/history").json(), before_history)

    def test_missing_or_forged_cookie_and_unknown_case_have_same_response(self):
        case_id = self.create(self.owner).json()["case_id"]
        expected = {"detail": "Case not found."}
        for cookie in (None, "forged", "A" * 43):
            self.other.cookies.clear()
            if cookie is not None:
                self.other.cookies.set(COOKIE_NAME, cookie)
            for identifier in (case_id, "not-a-case"):
                response = self.other.get(f"/cases/{identifier}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), expected)
                self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_cleared_cookie_cannot_reclaim_case_by_id(self):
        case_id = self.create(self.owner).json()["case_id"]
        self.owner.cookies.clear()
        self.create(self.owner)
        self.assertEqual(self.owner.get(f"/cases/{case_id}").status_code, 404)

    def test_unowned_legacy_case_remains_cli_accessible_and_web_private(self):
        create_case(self.database, sample("legacy"))
        self.create(self.owner)
        self.assertEqual(self.owner.get("/cases/legacy").status_code, 404)
        self.assertEqual(get_case(self.database, "legacy").case_id, "legacy")

    def test_restart_preserves_access_only_with_the_original_cookie(self):
        case_id = self.create(self.owner).json()["case_id"]
        with TestClient(create_app(self.database), cookies=dict(self.owner.cookies)) as resumed:
            self.assertEqual(resumed.get(f"/cases/{case_id}").status_code, 200)
        with TestClient(create_app(self.database)) as stranger:
            self.assertEqual(stranger.get(f"/cases/{case_id}").status_code, 404)

    def test_expired_session_is_denied_and_new_session_does_not_inherit_cases(self):
        case_id = self.create(self.owner).json()["case_id"]
        old_token = self.owner.cookies.get(COOKIE_NAME)
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("UPDATE visitor_sessions SET expires_at = 0")
        self.assertEqual(self.owner.get(f"/cases/{case_id}").status_code, 404)
        new_id = self.create(self.owner).json()["case_id"]
        self.assertNotEqual(self.owner.cookies.get(COOKIE_NAME), old_token)
        self.assertEqual(self.owner.get(f"/cases/{case_id}").status_code, 404)
        self.assertEqual(self.owner.get(f"/cases/{new_id}").status_code, 200)

    def test_cross_origin_null_and_cross_site_writes_are_denied(self):
        for headers in (
            {"Origin": "https://attacker.example"},
            {"Origin": DEFAULT_ORIGIN + ".attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                response = self.owner.post("/cases", json=PAYLOAD, headers=headers)
                self.assertEqual(response.status_code, 403)
        response = self.owner.post("/cases", json=PAYLOAD, headers={"Origin": DEFAULT_ORIGIN})
        self.assertEqual(response.status_code, 201)

    def test_simple_content_types_cannot_create_cases(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            response = self.owner.post("/cases", content=json.dumps(PAYLOAD), headers={"Content-Type": content_type})
            self.assertEqual(response.status_code, 415, response.text)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 0)

    def test_cross_origin_writes_with_owner_cookie_are_rejected_before_changes(self):
        case_id = self.create(self.owner).json()["case_id"]
        url = f"/cases/{case_id}"
        before = self.owner.get(url).json()
        history = self.owner.get(url + "/history").json()
        token = self.owner.get(url + "/next-step").json()["decision_token"]
        bodies = {
            "answers": {"event_id": "csrf-answer", "entry": {"question_id": "clarify_symptom", "symptom": "packet_loss"}},
            "observations": {"event_id": "csrf-reading", "entry": READING},
            "tests": {"event_id": "csrf-test", "entry": {"test_id": "compare_data_centers", "conditions": "Synthetic", "outcome": "improved", "notes": "Synthetic"}},
            "explanation": {"decision_token": token},
        }
        with patch(PROVIDER, new_callable=AsyncMock) as provider:
            for suffix, body in bodies.items():
                response = self.owner.post(
                    f"{url}/{suffix}", json=body,
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(response.status_code, 403, response.text)
            provider.assert_not_awaited()
        self.assertEqual(self.owner.get(url).json(), before)
        self.assertEqual(self.owner.get(url + "/history").json(), history)

    def test_client_cannot_supply_ownership_fields(self):
        response = self.owner.post("/cases", json={**PAYLOAD, "owner_id": "someone-else"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(COOKIE_NAME, self.owner.cookies)

    def test_cookie_flags_and_production_https(self):
        response = self.create(self.owner)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/", cookie)
        self.assertNotIn("domain=", cookie)
        with TestClient(create_app(self.database, public_origin="https://apex.example"), base_url="https://apex.example") as secure:
            response = self.create(secure)
            self.assertIn("; secure", response.headers["set-cookie"].lower())
            self.assertTrue(response.headers["set-cookie"].startswith("__Host-" + COOKIE_NAME + "="))
            self.assertEqual(secure.get("/cases/" + response.json()["case_id"]).status_code, 200)

    def test_health_stays_public_and_does_not_issue_session(self):
        response = self.other.get("/health")
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn("set-cookie", response.headers)


class OriginConfigurationTests(unittest.TestCase):
    def test_public_http_and_origin_urls_with_extra_components_are_rejected(self):
        for value in ("http://apex.example", "https://user:pass@apex.example", "https://apex.example/path", "https://apex.example?x=1", "null"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_origin(value)

    def test_https_and_loopback_origins_are_normalised(self):
        self.assertEqual(validate_public_origin("https://APEX.example:443/"), "https://apex.example")
        self.assertEqual(validate_public_origin(DEFAULT_ORIGIN), DEFAULT_ORIGIN)
