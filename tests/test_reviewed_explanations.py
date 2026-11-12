import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.api.explanations import build_explanation_router
from backend.app.explanation_service import explain_decision
from backend.app.knowledge import GroundedDecision
from backend.app.llm import MODEL, PROMPT_VERSION, ModelExplanation


TARGET = "backend.app.api.explanations.generate_explanation"


def sample(kind="report"):
    return GroundedDecision(
        case_id=kind,
        case_origin="synthetic",
        kind=kind,
        action_id=(
            "review_verification"
            if kind == "report"
            else "fixture_action"
        ),
        message="Summarise the verification readings and remaining symptoms.",
        reason="The most recently recorded verification outcome is 'improved'.",
        source_ids=[],
        knowledge_revision="test-revision",
        guidance=[],
    )


class ExplanationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_preserves_text_without_calling_model(self):
        decision = sample()
        before = decision.model_dump()
        provider = AsyncMock(
            side_effect=AssertionError("Model must not run")
        )

        result = await explain_decision(decision, generator=provider)

        provider.assert_not_awaited()
        self.assertEqual(
            result.explanation,
            f"{decision.message}\n\n{decision.reason}",
        )
        self.assertEqual(result.source, "reviewed_guidance")
        self.assertIsNone(result.model)
        self.assertIsNone(result.prompt_version)
        self.assertEqual(decision.model_dump(), before)

    async def test_report_is_not_truncated_by_model_word_limit(self):
        decision = sample().model_copy(
            update={"message": " ".join(["Review"] * 90)}
        )
        provider = AsyncMock(
            side_effect=AssertionError("Model must not run")
        )

        result = await explain_decision(decision, generator=provider)

        self.assertEqual(
            result.explanation,
            f"{decision.message}\n\n{decision.reason}",
        )
        provider.assert_not_awaited()

    async def test_questions_and_tests_keep_model_provenance(self):
        for kind in ("question", "test"):
            with self.subTest(kind=kind):
                decision = sample(kind)
                provider = AsyncMock(
                    return_value=ModelExplanation(
                        explanation="Existing step."
                    )
                )

                result = await explain_decision(
                    decision,
                    generator=provider,
                )

                provider.assert_awaited_once_with(decision)
                self.assertEqual(result.source, "model_selected_guidance")
                self.assertEqual(result.model, MODEL)
                self.assertEqual(result.prompt_version, PROMPT_VERSION)


class ReviewedRouteTests(unittest.TestCase):
    def setUp(self):
        self.decisions = {
            kind: sample(kind)
            for kind in ("report", "question")
        }
        self.report_reads = 0
        self.change_report = False

        def get_decision(case_id):
            if case_id not in self.decisions:
                raise HTTPException(404, "Case not found")

            decision = self.decisions[case_id].model_copy(deep=True)

            if case_id == "report":
                self.report_reads += 1

                if self.change_report and self.report_reads > 1:
                    decision.reason = "The recorded outcome changed."

            return decision

        app = FastAPI()
        app.include_router(build_explanation_router(get_decision))
        self.client = self.enterContext(TestClient(app))

    def request(self, case_id, token=None):
        return self.client.post(
            f"/cases/{case_id}/explanation",
            json={
                "decision_token": (
                    token or self.decisions[case_id].decision_token
                )
            },
        )

    def test_report_route_labels_reviewed_text(self):
        with patch(TARGET, new_callable=AsyncMock) as provider:
            response = self.request("report")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "reviewed_guidance")
        self.assertIsNone(body["model"])
        self.assertIsNone(body["prompt_version"])
        provider.assert_not_awaited()

    def test_stale_report_is_rejected(self):
        with patch(TARGET, new_callable=AsyncMock) as provider:
            response = self.request("report", "0" * 64)

        self.assertEqual(response.status_code, 409)
        provider.assert_not_awaited()

    def test_changed_report_is_discarded(self):
        self.change_report = True

        with patch(TARGET, new_callable=AsyncMock) as provider:
            response = self.request("report")

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("explanation", response.json())
        provider.assert_not_awaited()

    def test_report_works_while_model_is_busy(self):
        started, release = Event(), Event()

        async def slow_model(decision):
            started.set()

            async with asyncio.timeout(5):
                while not release.is_set():
                    await asyncio.sleep(0.01)

            return ModelExplanation(explanation="Existing step.")

        with patch(TARGET, new_callable=AsyncMock) as provider:
            provider.side_effect = slow_model

            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.request, "question")

                try:
                    self.assertTrue(started.wait(5))

                    report = self.request("report")
                    self.assertEqual(report.status_code, 200)
                    self.assertEqual(
                        report.json()["source"],
                        "reviewed_guidance",
                    )

                    self.assertEqual(
                        self.request("question").status_code,
                        429,
                    )
                finally:
                    release.set()

                self.assertEqual(
                    pending.result(timeout=5).status_code,
                    200,
                )

            provider.assert_awaited_once()
