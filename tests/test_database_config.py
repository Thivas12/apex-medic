import os
import traceback
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import psycopg

from backend.app.db.config import (
    DatabaseConfigurationError, PostgresDatabase, StorageUnavailable, resolve_database,
)
from backend.app.db.postgres import transaction


URL = "postgresql://tester:NOT_A_REAL_PASSWORD@ep-example-pooler.test/db?sslmode=require"
DIRECT = URL.replace("-pooler", "")


class DatabaseConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory())
        self.file = Path(self.directory) / ".env.local"
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_env_file_is_loaded_without_exporting_or_interpolating_secrets(self):
        url = URL.replace("NOT_A_REAL_PASSWORD", "literal${TOKEN}")
        self.file.write_text(f"DATABASE_URL='{url}'\nDATABASE_URL_UNPOOLED='{DIRECT}'\n", encoding="utf-8")
        database = resolve_database(env_file=self.file)
        self.assertEqual(database.url, url)
        self.assertNotIn("DATABASE_URL", os.environ)
        self.assertNotIn("literal", repr(database))

    def test_environment_overrides_file(self):
        self.file.write_text(f"DATABASE_URL={URL}\n", encoding="utf-8")
        with patch.dict(os.environ, DATABASE_URL=DIRECT):
            self.assertEqual(resolve_database(env_file=self.file).url, DIRECT)

    def test_explicit_sqlite_path_ignores_environment(self):
        with patch.dict(os.environ, DATABASE_URL="invalid-private-value"):
            path = Path(self.directory) / "test.sqlite3"
            self.assertEqual(resolve_database(path), path)

    def test_sqlite_fallback_and_override(self):
        self.assertIsInstance(resolve_database(env_file=self.file), Path)
        with patch.dict(os.environ, APEX_DATABASE_BACKEND="sqlite", DATABASE_URL=URL):
            self.assertIsInstance(resolve_database(env_file=self.file), Path)

    def test_bad_or_empty_postgres_configuration_never_falls_back(self):
        for value in ("", "secret-invalid-url", URL.replace("require", "disable")):
            with self.subTest(value=bool(value)), patch.dict(os.environ, DATABASE_URL=value):
                with self.assertRaises(DatabaseConfigurationError):
                    resolve_database(env_file=self.file)
        with patch.dict(os.environ, APEX_DATABASE_BACKEND="postgres"):
            with self.assertRaises(DatabaseConfigurationError):
                resolve_database(env_file=self.file)

    def test_migrations_require_matching_direct_endpoint(self):
        for direct in (None, URL, DIRECT.replace("/db", "/other"), DIRECT.replace("ep-example", "ep-other")):
            with self.subTest(direct_present=direct is not None):
                with self.assertRaises(DatabaseConfigurationError):
                    PostgresDatabase(URL, direct).migration_database()
        self.assertEqual(PostgresDatabase(URL, DIRECT).migration_database().url, DIRECT)

    def test_invalid_url_error_does_not_leak_input(self):
        private_value = "SECRET-CONNECTION-VALUE"
        try:
            PostgresDatabase(private_value)
        except DatabaseConfigurationError:
            self.assertNotIn("SECRET-CONNECTION-VALUE", traceback.format_exc())
        else:
            self.fail("Expected a sanitized configuration error")

    def test_driver_error_is_sanitized(self):
        with patch("backend.app.db.postgres.psycopg.connect", side_effect=psycopg.OperationalError("PRIVATE-CREDENTIAL")):
            try:
                with transaction(PostgresDatabase(URL)):
                    self.fail("Connection should fail")
            except StorageUnavailable:
                self.assertNotIn("PRIVATE-CREDENTIAL", traceback.format_exc())
