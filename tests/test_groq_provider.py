import asyncio
import io
import json
import os
import traceback
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.explanations import build_explanation_router
from backend.app.check_model import check
from backend.app.explanation_service import explain_decision
from backend.app.llm import ExplanationFailure, ModelExplanation, generate_explanation
from backend.app.model_config import (
    GROQ_MODEL, ModelConfigurationError, ModelSettings, load_model_settings,
)
from test_explanations import sample_decision


SETTINGS = ModelSettings(provider="groq", api_key="synthetic-test-key")


def reply(content='{"option_id":"step"}', **message_changes):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": content, **message_changes,
    }}]}


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(TemporaryDirectory())
        self.file = Path(self.folder) / ".env.local"
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_default_remains_local_and_ignores_cloud_key(self):
        self.file.write_text("GROQ_API_KEY=synthetic-key\n", encoding="utf-8")
        self.assertEqual(load_model_settings(env_file=self.file), ModelSettings())

    def test_file_settings_and_environment_precedence(self):
        self.file.write_text("APEX_LLM_PROVIDER=groq\nGROQ_API_KEY='literal${TOKEN}'\n", encoding="utf-8")
        settings = load_model_settings(env_file=self.file)
        self.assertEqual(settings.provider, "groq")
        self.assertEqual(settings.api_key, "literal${TOKEN}")
        self.assertNotIn(settings.api_key, repr(settings))
        self.assertNotIn("GROQ_API_KEY", os.environ)
        with patch.dict(os.environ, APEX_LLM_PROVIDER="ollama"):
            self.assertEqual(load_model_settings(env_file=self.file), ModelSettings())

    def test_missing_empty_or_header_injection_key_is_rejected(self):
        for value in ("", "bad\nheader", "contains space", "தமிழ்"):
            with patch.dict(os.environ, APEX_LLM_PROVIDER="groq", GROQ_API_KEY=value):
                with self.assertRaises(ModelConfigurationError):
                    load_model_settings(env_file=self.file)
        with patch.dict(os.environ, APEX_LLM_PROVIDER="unsupported"):
            with self.assertRaises(ModelConfigurationError):
                load_model_settings(env_file=self.file)


class GroqProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_is_minimal_structured_and_uses_fixed_host(self):
        decision = sample_decision()
        before = decision.model_dump_json()
        def handler(request):
            self.assertEqual(str(request.url), "https://api.groq.com/openai/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], GROQ_MODEL)
            self.assertTrue(payload["response_format"]["json_schema"]["strict"])
            self.assertFalse(payload["response_format"]["json_schema"]["schema"]["additionalProperties"])
            self.assertEqual(payload["max_completion_tokens"], 1024)
            self.assertFalse(payload["include_reasoning"])
            self.assertEqual(payload["reasoning_effort"], "low")
            for key in ("tools", "keep_alive", "options"):
                self.assertNotIn(key, payload)
            context = json.loads(payload["messages"][1]["content"])
            self.assertEqual(set(context), {"kind", "action_id", "message", "reason", "options"})
            self.assertNotIn(SETTINGS.api_key, request.content.decode())
            return httpx.Response(200, json=reply())
        result = await generate_explanation(decision, settings=SETTINGS, transport=httpx.MockTransport(handler))
        self.assertEqual(result._provider_name, "groq")
        self.assertEqual(result._model_name, GROQ_MODEL)
        self.assertEqual(set(result.model_dump()), {"explanation"})
        self.assertEqual(decision.model_dump_json(), before)

    async def test_bad_truncated_refused_and_tool_outputs_are_rejected(self):
        partial = reply()
        partial["choices"][0]["finish_reason"] = "length"
        variants = [
            {}, [], {"choices": []}, {"choices": [None]}, partial,
            reply("not-json"), reply('{"option_id":" "}'),
            reply('{"option_id":"step","diagnosis":"router"}'),
            reply(json.dumps({"option_id": "x" * 65})),
            reply('{"option_id":"other_action_guidance"}'),
            reply(content=None), reply(tool_calls=[{}]), reply(function_call={"name": "test"}),
            reply(refusal="No"), reply(role="user"),
            reply('{"option_id":"step","_provider_name":"ollama"}'),
        ]
        for envelope in variants:
            with self.subTest(envelope_type=type(envelope).__name__):
                with self.assertRaises(ExplanationFailure) as caught:
                    await generate_explanation(sample_decision(), settings=SETTINGS,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope)))
                self.assertEqual(caught.exception.status_code, 502)

    async def test_quota_is_not_retried_and_delay_is_bounded(self):
        for value, expected in (("7", 7), ("999999", 86400), ("-1", 60), ("invalid", 60), ("0", 1)):
            count = 0
            def handler(request):
                nonlocal count
                count += 1
                return httpx.Response(429, headers={"retry-after": value}, json={"error": "PRIVATE"})
            with self.assertRaises(ExplanationFailure) as caught:
                await generate_explanation(sample_decision(), settings=SETTINGS, transport=httpx.MockTransport(handler))
            self.assertEqual(count, 1)
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(caught.exception.retry_after, expected)
            self.assertNotIn("PRIVATE", str(caught.exception))

    async def test_auth_errors_and_redirects_never_expose_or_forward_credentials(self):
        for status in (301, 307, 400, 401, 403, 500):
            calls = []
            def handler(request):
                calls.append(request.url.host)
                return httpx.Response(status, headers={"location": "https://untrusted.invalid/"},
                                      json={"error": "PRIVATE-UPSTREAM-TEXT"})
            with self.assertRaises(ExplanationFailure) as caught:
                await generate_explanation(sample_decision(), settings=SETTINGS, transport=httpx.MockTransport(handler))
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(calls, ["api.groq.com"])
            self.assertNotIn("PRIVATE-UPSTREAM-TEXT", str(caught.exception))

    async def test_timeout_and_network_failures_are_sanitized(self):
        for failure, expected in ((httpx.ReadTimeout, 504), (httpx.ConnectError, 503)):
            def handler(request):
                raise failure("PRIVATE-KEY-IN-ERROR", request=request)
            try:
                await generate_explanation(sample_decision(), settings=SETTINGS, transport=httpx.MockTransport(handler))
            except ExplanationFailure as error:
                self.assertEqual(error.status_code, expected)
                self.assertNotIn("PRIVATE-KEY-IN-ERROR", traceback.format_exc())
            else:
                self.fail("Provider failure must be rejected")

    async def test_cloud_deadline_is_enforced(self):
        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=reply())
        with patch("backend.app.llm.DEADLINE_SECONDS", .01):
            with self.assertRaises(ExplanationFailure) as caught:
                await generate_explanation(sample_decision(), settings=SETTINGS, transport=httpx.MockTransport(handler))
        self.assertEqual(caught.exception.status_code, 504)

    async def test_missing_configuration_makes_no_network_call(self):
        def handler(request):
            self.fail("No request should be made")
        with patch("backend.app.llm.load_model_settings", side_effect=ModelConfigurationError("Key missing.")):
            with self.assertRaises(ExplanationFailure) as caught:
                await generate_explanation(sample_decision(), transport=httpx.MockTransport(handler))
        self.assertEqual(caught.exception.status_code, 503)

    async def test_real_adapter_metadata_survives_service_and_report_skips_provider(self):
        async def generator(decision):
            return await generate_explanation(decision, settings=SETTINGS,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply())))
        result = await explain_decision(sample_decision(), generator=generator)
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.source, "model_selected_guidance")
        self.assertEqual(result.model, GROQ_MODEL)
        report = sample_decision().model_copy(update={"kind": "report"})
        unused = AsyncMock(side_effect=AssertionError("Provider must not run"))
        result = await explain_decision(report, generator=unused)
        self.assertIsNone(result.provider)
        self.assertIsNone(result.model)
        unused.assert_not_awaited()

    async def test_config_only_check_makes_no_request_or_prints_key(self):
        output = io.StringIO()
        with patch("backend.app.check_model.load_model_settings", return_value=SETTINGS), \
             patch("backend.app.check_model.generate_explanation", new_callable=AsyncMock) as provider, \
             redirect_stdout(output):
            self.assertEqual(await check(config_only=True), 0)
        provider.assert_not_awaited()
        self.assertIn("Provider: groq", output.getvalue())
        self.assertNotIn(SETTINGS.api_key, output.getvalue())


class GroqRouteTests(unittest.TestCase):
    def test_quota_response_forwards_retry_header_and_releases_lock(self):
        decision = sample_decision()
        app = FastAPI()
        app.include_router(build_explanation_router(lambda case_id: decision))
        with TestClient(app) as client, patch("backend.app.api.explanations.generate_explanation", new=AsyncMock(side_effect=[
            ExplanationFailure(429, "Cloud quota reached.", retry_after=7),
            ModelExplanation(explanation="Record the missing reading."),
        ])):
            path = f"/cases/{decision.case_id}/explanation"
            payload = {"decision_token": decision.decision_token}
            response = client.post(path, json=payload)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["retry-after"], "7")
            self.assertEqual(client.post(path, json=payload).status_code, 200)
