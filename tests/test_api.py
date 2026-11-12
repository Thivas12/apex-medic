from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from backend.app.api.main import create_app


ANSWER = {
    "question_id": "clarify_symptom",
    "symptom": "packet_loss",
}

READING = {
    "source": "player_report",
    "context": "Synthetic API reading",
    "latency_ms": 55,
    "packet_loss_pct": 8,
    "fps": 144,
}

RESULT = {
    "test_id": "compare_data_centers",
    "conditions": "Synthetic comparison with the original affected baseline",
    "outcome": "improved",
    "notes": "Synthetic evidence; cause remains unknown.",
}


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.db = Path(temporary.name) / "cases.sqlite3"
        self.client = self.enterContext(
            TestClient(create_app(self.db))
        )
    def test_explanation_does_not_change_case_or_history(self):
        from unittest.mock import AsyncMock, patch
        from backend.app.llm import ModelExplanation

        case_id = self.new_case(symptom="packet_loss")
        case_url = f"/cases/{case_id}"

        before_case = self.client.get(case_url).json()
        before_history = self.client.get(
            f"{case_url}/history"
        ).json()

        decision = self.client.get(
            f"{case_url}/next-step"
        ).json()

        mock_model = AsyncMock(
            return_value=ModelExplanation(
                explanation="Record the missing packet-loss reading.",
            )
        )

        with patch(
            "backend.app.api.explanations.generate_explanation",
            new=mock_model,
        ):
            response = self.client.post(
                f"{case_url}/explanation",
                json={
                    "decision_token": decision["decision_token"],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["case_id"], case_id)
        mock_model.assert_awaited_once()

        self.assertEqual(
            self.client.get(case_url).json(),
            before_case,
        )
        self.assertEqual(
            self.client.get(f"{case_url}/history").json(),
            before_history,
        )

    def new_case(self, **changes):
        response = self.client.post(
            "/cases",
            json={
                "origin": "synthetic",
                "player_description": "Synthetic API case",
                "connection_type": "ethernet",
                **changes,
            },
        )

        self.assertEqual(
            response.status_code,
            201,
            response.text,
        )

        return response.json()["case_id"]

    def test_next_step_includes_reviewed_guidance(self):
        case_id = self.new_case(symptom="packet_loss")

        response = self.client.get(
            f"/cases/{case_id}/next-step"
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(
            data["action_id"],
            "collect_performance_readings",
        )
        self.assertEqual(
            data["guidance"][0]["card_id"],
            "performance_display",
        )
        self.assertEqual(
            data["guidance"][0]["citations"][0]["source_id"],
            "ea_connection_guide",
        )
        self.assertEqual(len(data["knowledge_revision"]), 64)

    def submit(self, case_id, route, entry, event_id):
        response = self.client.post(
            f"/cases/{case_id}/{route}",
            json={
                "event_id": event_id,
                "entry": entry,
            },
        )

        self.assertEqual(
            response.status_code,
            200,
            response.text,
        )

        return response.json()

    def next_action(self, case_id):
        response = self.client.get(
            f"/cases/{case_id}/next-step"
        )

        self.assertEqual(
            response.status_code,
            200,
            response.text,
        )

        return response.json()["action_id"]

    def test_complete_http_flow(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        case_id = self.new_case()

        self.assertEqual(
            self.next_action(case_id),
            "clarify_symptom",
        )

        self.submit(case_id, "answers", ANSWER, "answer-1")

        self.assertEqual(
            self.next_action(case_id),
            "collect_performance_readings",
        )

        self.submit(
            case_id,
            "observations",
            READING,
            "reading-1",
        )

        self.assertEqual(
            self.next_action(case_id),
            "compare_data_centers",
        )

        self.submit(case_id, "tests", RESULT, "test-1")

        self.assertEqual(
            self.next_action(case_id),
            "verify_improvement",
        )

        self.submit(
            case_id,
            "tests",
            {**RESULT, "test_id": "verify_improvement"},
            "verify-1",
        )

        self.assertEqual(
            self.next_action(case_id),
            "review_verification",
        )

        response = self.client.get(f"/cases/{case_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["status"],
            "investigating",
        )
        self.assertIsNone(response.json()["confirmed_cause"])

        response = self.client.get(
            f"/cases/{case_id}/history"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 4)

    def test_retries_and_conflicts_preserve_evidence(self):
        case_id = self.new_case(symptom="packet_loss")

        first = self.submit(
            case_id,
            "observations",
            READING,
            "reading-1",
        )
        repeated = self.submit(
            case_id,
            "observations",
            READING,
            "reading-1",
        )

        self.assertTrue(first["added"])
        self.assertFalse(repeated["added"])

        response = self.client.post(
            f"/cases/{case_id}/observations",
            json={
                "event_id": "reading-1",
                "entry": {
                    **READING,
                    "packet_loss_pct": 3,
                },
            },
        )

        self.assertEqual(response.status_code, 409)

        response = self.client.post(
            f"/cases/{case_id}/answers",
            json={
                "event_id": "late-answer",
                "entry": {
                    **ANSWER,
                    "symptom": "high_latency",
                },
            },
        )

        self.assertEqual(response.status_code, 409)

        saved = self.client.get(
            f"/cases/{case_id}"
        ).json()

        self.assertEqual(saved["symptom"], "packet_loss")
        self.assertEqual(len(saved["observations"]), 1)
        self.assertEqual(
            saved["observations"][0]["packet_loss_pct"],
            8,
        )

        history = self.client.get(
            f"/cases/{case_id}/history"
        ).json()

        self.assertEqual(len(history), 1)

    def test_invalid_and_missing_requests(self):
        case_id = self.new_case()
        before = self.client.get(
            f"/cases/{case_id}"
        ).json()

        invalid = [
            (
                "observations",
                {**READING, "packet_loss_pct": 101},
            ),
            (
                "answers",
                {**ANSWER, "question_id": "wrong_question"},
            ),
        ]

        for route, entry in invalid:
            with self.subTest(route=route):
                response = self.client.post(
                    f"/cases/{case_id}/{route}",
                    json={
                        "event_id": "bad-entry",
                        "entry": entry,
                    },
                )

                self.assertEqual(response.status_code, 422)

        for suffix in ("", "/next-step", "/history"):
            with self.subTest(suffix=suffix):
                response = self.client.get(
                    f"/cases/missing{suffix}"
                )

                self.assertEqual(response.status_code, 404)

        response = self.client.post(
            "/cases/missing/observations",
            json={
                "event_id": "missing-case",
                "entry": READING,
            },
        )

        self.assertEqual(response.status_code, 404)

        response = self.client.post(
            "/cases",
            json={
                "player_description": "Synthetic forbidden fields",
                "case_id": "chosen-by-client",
                "status": "resolved",
                "confirmed_cause": "unverified cause",
            },
        )

        self.assertEqual(response.status_code, 422)

        self.assertEqual(
            self.client.get(f"/cases/{case_id}").json(),
            before,
        )
        self.assertEqual(
            self.client.get(
                f"/cases/{case_id}/history"
            ).json(),
            [],
        )

    def test_data_survives_a_new_app_instance(self):
        case_id = self.new_case()
        self.submit(case_id, "answers", ANSWER, "answer-1")

        with TestClient(create_app(self.db), cookies=dict(self.client.cookies)) as restarted:
            response = restarted.get(f"/cases/{case_id}")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["symptom"],
                "packet_loss",
            )
            self.assertEqual(
                len(
                    restarted.get(
                        f"/cases/{case_id}/history"
                    ).json()
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()