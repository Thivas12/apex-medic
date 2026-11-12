import asyncio
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.api.render_ingress import RenderIngress
from backend.app.hosting import HostingConfigurationError, render_settings
from backend.app.serve import main as serve


ORIGIN = "https://apexmedic-fixture.onrender.com"
ENV = {
    "RENDER": "true", "RENDER_SERVICE_TYPE": "web", "RENDER_EXTERNAL_URL": ORIGIN,
    "DATABASE_URL": "postgresql://fixture:synthetic-password@database.example/test?sslmode=require",
    "APEX_DATABASE_BACKEND": "postgres", "APEX_LLM_PROVIDER": "groq",
    "GROQ_API_KEY": "synthetic-test-key", "PORT": "10000",
}
PAYLOAD = {"origin": "synthetic", "player_description": "Synthetic hosted test"}
EDGE = {"CF-Connecting-IP": "192.0.2.10", "Origin": ORIGIN}


class RenderConfigurationTests(unittest.TestCase):
    def test_render_url_configures_https_and_hides_credentials(self):
        with patch.dict(os.environ, ENV, clear=True):
            settings = render_settings()
        self.assertEqual(settings.origin, ORIGIN)
        self.assertEqual(settings.allowed_hosts, ("apexmedic-fixture.onrender.com",))
        self.assertIsNone(settings.database.direct_url)
        self.assertNotIn("synthetic-password", repr(settings))
        self.assertNotIn(ENV["GROQ_API_KEY"], repr(settings))

    def test_incomplete_or_unsafe_hosting_settings_stop_startup_without_secrets(self):
        changes = [
            {"RENDER": "false"}, {"RENDER_SERVICE_TYPE": "pserv"},
            {"RENDER_EXTERNAL_URL": ""}, {"RENDER_EXTERNAL_URL": "https://unrelated.example"},
            {"APEX_PUBLIC_ORIGIN": "http://127.0.0.1:8000"},
            {"APEX_PUBLIC_ORIGIN": "https://localhost"},
            {"APEX_PUBLIC_ORIGIN": "https://secret-in-url@site.example"},
            {"APEX_DATABASE_BACKEND": "sqlite"}, {"DATABASE_URL": ""},
            {"DATABASE_URL": "postgresql://fixture:synthetic-password@database.example/test"},
            {"APEX_LLM_PROVIDER": "ollama"}, {"GROQ_API_KEY": ""},
            {"GROQ_API_KEY": "synthetic secret with spaces"}, {"PORT": "0"}, {"PORT": "invalid"},
        ]
        for change in changes:
            with self.subTest(setting=next(iter(change))):
                with patch.dict(os.environ, ENV | change, clear=True):
                    with self.assertRaises(HostingConfigurationError) as caught:
                        render_settings()
                    self.assertNotIn("synthetic-password", str(caught.exception))
                    self.assertNotIn("secret-in-url", str(caught.exception))
                    with patch("backend.app.serve.uvicorn.run") as run, redirect_stdout(io.StringIO()):
                        self.assertEqual(serve(), 1)
                        run.assert_not_called()

    def test_custom_origin_keeps_the_render_alias_as_an_allowed_host(self):
        with patch.dict(os.environ, ENV | {"APEX_PUBLIC_ORIGIN": "https://apex.example/"}, clear=True):
            settings = render_settings()
        self.assertEqual(settings.origin, "https://apex.example")
        self.assertEqual(settings.allowed_hosts, ("apex.example", "apexmedic-fixture.onrender.com"))

    def test_startup_overrides_proxy_and_worker_defaults_and_disables_access_logging(self):
        with patch.dict(os.environ, ENV | {"FORWARDED_ALLOW_IPS": "*", "WEB_CONCURRENCY": "4"}, clear=True):
            with patch("backend.app.serve.uvicorn.run") as run:
                self.assertEqual(serve(), 0)
        options = run.call_args.kwargs
        self.assertFalse(options["proxy_headers"])
        self.assertEqual(options["forwarded_allow_ips"], "")
        self.assertEqual(options["workers"], 1)
        self.assertFalse(options["reload"])
        self.assertFalse(options["access_log"])
        self.assertEqual(options["port"], 10000)


class RenderRequestTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "isolated.sqlite3"
        app = create_app(self.path, public_origin=ORIGIN)
        app.add_middleware(RenderIngress, allowed_hosts=("apexmedic-fixture.onrender.com",))
        self.client = self.enterContext(TestClient(app, base_url=ORIGIN))

    def test_root_and_ui_redirects_stay_https(self):
        root = self.client.get("/", headers=EDGE, follow_redirects=False)
        self.assertEqual(root.status_code, 307)
        self.assertEqual(root.headers["location"], "/ui/")
        response = self.client.get("/ui", headers=EDGE, follow_redirects=False)
        self.assertEqual(response.headers["location"], ORIGIN + "/ui/")
        self.assertEqual(self.client.get("/ui/", headers=EDGE).status_code, 200)

    def test_public_origin_cookie_and_case_ownership_survive_the_edge(self):
        response = self.client.post("/cases", headers=EDGE, json=PAYLOAD)
        self.assertEqual(response.status_code, 201, response.text)
        cookie = response.headers["set-cookie"].lower()
        self.assertTrue(cookie.startswith("__host-apexmedic_session="))
        for flag in ("secure", "httponly", "samesite=strict", "path=/"):
            self.assertIn(flag, cookie)
        url = "/cases/" + response.json()["case_id"]
        self.assertEqual(self.client.get(url, headers=EDGE).status_code, 200)
        self.assertEqual(self.client.post(url + "/answers", json={
            "event_id": "hosted-answer", "entry": {"question_id": "clarify_symptom", "symptom": "packet_loss"},
        }, headers=EDGE).status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(url, headers=EDGE).status_code, 404)

    def test_forged_forwarding_headers_do_not_reset_a_visitor_budget(self):
        for index in range(5):
            response = self.client.post("/cases", json=PAYLOAD, headers=EDGE | {
                "X-Forwarded-For": f"198.51.100.{index}", "X-Real-IP": f"203.0.113.{index}",
                "Forwarded": f"for=198.51.100.{index}", "X-Forwarded-Proto": "http",
            })
            self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.post("/cases", headers=EDGE, json=PAYLOAD).status_code, 429)
        # Same proxy, different edge-authenticated visitor: distinct network budget.
        other = EDGE | {"CF-Connecting-IP": "192.0.2.11"}
        self.assertEqual(self.client.post("/cases", headers=other, json=PAYLOAD).status_code, 201)

    def test_invalid_or_missing_edge_address_is_rejected_before_case_creation(self):
        variants = [{}, {"CF-Connecting-IP": "invalid"}, {"CF-Connecting-IP": "192.0.2.1, 192.0.2.2"},
                    {"CF-Connecting-IP": "fe80::1%interface"}, {"CF-Connecting-IP": ""}]
        for headers in variants:
            with self.subTest(headers=headers):
                response = self.client.post("/cases", json=PAYLOAD, headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("set-cookie", response.headers)
        for _ in range(5):
            self.assertEqual(self.client.post("/cases", json=PAYLOAD, headers=EDGE).status_code, 201)

    def test_duplicate_edge_addresses_and_untrusted_hosts_are_rejected(self):
        duplicate = [("CF-Connecting-IP", "192.0.2.1"), ("CF-Connecting-IP", "192.0.2.2")]
        self.assertEqual(self.client.post("/cases", json=PAYLOAD, headers=duplicate).status_code, 503)
        response = self.client.get("/ui", headers=EDGE | {"Host": "untrusted.example"}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("location", response.headers)

    def test_other_origins_cannot_write_even_with_an_edge_address(self):
        self.assertEqual(self.client.post("/cases", json=PAYLOAD, headers=EDGE | {
            "Origin": "https://untrusted.example",
        }).status_code, 403)

    def test_health_probe_needs_no_edge_headers_and_issues_no_cookie(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn("set-cookie", response.headers)

    def test_edge_normalization_does_not_mutate_the_input_scope(self):
        captured = []
        async def inner(scope, receive, send):
            captured.append(scope)
        original = {"type": "http", "path": "/ui/", "method": "GET", "scheme": "http",
                    "client": ("10.0.0.1", 100), "headers": [
                        (b"host", b"apexmedic-fixture.onrender.com"),
                        (b"cf-connecting-ip", b"2001:db8::1"),
                    ]}
        asyncio.run(RenderIngress(inner, allowed_hosts=("apexmedic-fixture.onrender.com",))(original, None, None))
        self.assertEqual(original["client"], ("10.0.0.1", 100))
        self.assertEqual(original["scheme"], "http")
        self.assertEqual(captured[0]["client"], ("2001:db8::1", 0))
        self.assertEqual(captured[0]["scheme"], "https")
