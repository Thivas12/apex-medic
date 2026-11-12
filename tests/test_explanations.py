import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.agent.rules import Decision
from backend.app.api.explanations import build_explanation_router
from backend.app.knowledge import ground_decision, load_knowledge
from backend.app.llm import (
    ExplanationFailure,
    ModelExplanation,
    generate_explanation,
)


ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "backend.app.api.explanations.generate_explanation"


def sample_decision():
    return ground_decision(
        Decision(
            case_id="model_test",
            case_origin="synthetic",
            kind="question",
            action_id="collect_performance_readings",
            message="Record readings during an affected match.",
            reason="Packet-loss readings are missing.",
            source_ids=["ea_connection_guide"],
        ),
        load_knowledge(ROOT / "knowledge"),
    )


def model_reply(content, **changes):
    return {
        "done": True,
        "done_reason": "stop",
        "message": {
            "role": "assistant",
            "content": content,
        },
        **changes,
    }


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from backend.app.model_config import ModelSettings
        self.enterContext(patch("backend.app.llm.load_model_settings", return_value=ModelSettings()))

    async def test_valid_reply_uses_recommendation_without_mutating_it(self):
        decision = sample_decision()
        before = decision.model_dump_json()

        def handler(request):
            body = json.loads(request.content)
            self.assertNotIn("tools", body)

            supplied = json.loads(
                body["messages"][1]["content"]
            )
            self.assertEqual(
                supplied["message"],
                decision.message,
            )
            self.assertNotIn("player_description", supplied)

            return httpx.Response(
                200,
                json=model_reply(
                    '{"option_id":"guidance_1"}'
                ),
            )

        result = await generate_explanation(
            decision,
            transport=httpx.MockTransport(handler),
        )

        self.assertIn("packet loss", result.explanation)
        self.assertEqual(decision.model_dump_json(), before)

    async def test_invalid_or_incomplete_output_is_rejected(self):
        variants = [
            model_reply("not JSON"),
            model_reply('{"option_id":"   "}'),
            model_reply(
                '{"option_id":"step","confirmed_cause":"router"}'
            ),
            model_reply(
                json.dumps({"option_id": "unavailable_option"})
            ),
            model_reply(
                '{"option_id":"step"}',
                done_reason="length",
            ),
            model_reply(
                "{}",
                message={
                    "role": "assistant",
                    "content": "{}",
                    "tool_calls": [{}],
                },
            ),
        ]

        for envelope in variants:
            with self.subTest(envelope=envelope):
                transport = httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json=envelope,
                    )
                )

                with self.assertRaises(ExplanationFailure) as caught:
                    await generate_explanation(
                        sample_decision(),
                        transport=transport,
                    )

                self.assertEqual(
                    caught.exception.status_code,
                    502,
                )

    async def test_connection_and_read_failures_are_classified(self):
        variants = (
            (httpx.ConnectError, 503),
            (httpx.ReadTimeout, 504),
        )

        for error_class, status in variants:
            with self.subTest(error=error_class):
                def handler(request):
                    raise error_class(
                        "Synthetic failure",
                        request=request,
                    )

                with self.assertRaises(ExplanationFailure) as caught:
                    await generate_explanation(
                        sample_decision(),
                        transport=httpx.MockTransport(handler),
                    )

                self.assertEqual(
                    caught.exception.status_code,
                    status,
                )

    async def test_overall_deadline_is_enforced(self):
        async def slow_handler(request):
            await asyncio.sleep(1)
            return httpx.Response(
                200,
                json=model_reply('{"option_id":"step"}'),
            )

        with patch("backend.app.llm.DEADLINE_SECONDS", 0.01):
            with self.assertRaises(ExplanationFailure) as caught:
                await generate_explanation(
                    sample_decision(),
                    transport=httpx.MockTransport(slow_handler),
                )

        self.assertEqual(caught.exception.status_code, 504)


class ExplanationRouteTests(unittest.TestCase):
    def setUp(self):
        self.decision = sample_decision()

        def get_decision(case_id):
            if case_id != self.decision.case_id:
                raise HTTPException(404, "Case not found.")

            return self.decision.model_copy(deep=True)

        app = FastAPI()
        app.include_router(
            build_explanation_router(get_decision)
        )

        self.client = self.enterContext(TestClient(app))

    def request_explanation(self):
        return self.client.post(
            "/cases/model_test/explanation",
            json={
                "decision_token": self.decision.decision_token,
            },
        )

    def test_stale_or_missing_case_does_not_call_model(self):
        with patch(PROVIDER, new_callable=AsyncMock) as model:
            stale = self.client.post(
                "/cases/model_test/explanation",
                json={"decision_token": "0" * 64},
            )
            missing = self.client.post(
                "/cases/missing/explanation",
                json={
                    "decision_token": self.decision.decision_token,
                },
            )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(missing.status_code, 404)
        model.assert_not_awaited()

    def test_provider_errors_release_capacity_for_the_next_request(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                outcomes = [
                    ExplanationFailure(
                        status,
                        "Synthetic model failure",
                    ),
                    ModelExplanation(
                        explanation="Record the missing reading.",
                    ),
                ]

                with patch(
                    PROVIDER,
                    new=AsyncMock(side_effect=outcomes),
                ):
                    self.assertEqual(
                        self.request_explanation().status_code,
                        status,
                    )
                    self.assertEqual(
                        self.request_explanation().status_code,
                        200,
                    )

    def test_changed_recommendation_discards_generated_reply(self):
        async def change_during_generation(decision):
            self.decision = self.decision.model_copy(
                update={"message": "A different next step."},
            )

            return ModelExplanation(
                explanation="This answer used the old recommendation.",
            )

        with patch(
            PROVIDER,
            new=AsyncMock(side_effect=change_during_generation),
        ):
            response = self.request_explanation()

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("explanation", response.json())

    def test_concurrent_generation_is_rejected_without_queuing(self):
        started = Event()
        release = Event()

        async def waiting_model(decision):
            started.set()

            while not release.is_set():
                await asyncio.sleep(0.01)

            return ModelExplanation(
                explanation="Record the missing reading.",
            )

        with patch(
            PROVIDER,
            new=AsyncMock(side_effect=waiting_model),
        ):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.request_explanation)

                try:
                    self.assertTrue(started.wait(5))
                    self.assertEqual(
                        self.request_explanation().status_code,
                        429,
                    )
                finally:
                    release.set()

                self.assertEqual(
                    first.result(timeout=5).status_code,
                    200,
                )


if __name__ == "__main__":
    unittest.main()
