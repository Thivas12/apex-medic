import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from backend.app.db.repository import (
    append_evidence,
    create_case,
    get_case,
    get_history,
    initialise_database,
)
from backend.app.models.case import Case


ROOT = Path(__file__).resolve().parents[1]

OBS = {
    "source": "player_report",
    "context": "Synthetic follow-up measurement",
    "latency_ms": 52,
    "packet_loss_pct": 2,
    "fps": 144,
}

RESULT = {
    "test_id": "compare_data_centers",
    "conditions": "Synthetic comparison using the same wired connection",
    "outcome": "improved",
    "notes": "Synthetic result; improvement does not confirm the cause.",
}


class CaseUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.db = Path(temporary.name) / "cases.sqlite3"
        fixture = ROOT / "evaluation/dev_cases/packet_loss.json"

        self.original = Case.model_validate_json(
            fixture.read_text(encoding="utf-8-sig")
        )
        self.case_id = self.original.case_id

        initialise_database(self.db)
        create_case(self.db, self.original)

    def add(self, kind, entry, event_id):
        return append_evidence(
            self.db,
            self.case_id,
            kind,
            json.dumps(entry),
            event_id,
        )

    def test_append_preserves_evidence_and_records_history(self):
        self.add("observation", OBS, "obs-1")
        self.add("test", RESULT, "test-1")

        saved = get_case(self.db, self.case_id)

        self.assertEqual(
            saved.observations[:-1],
            self.original.observations,
        )
        self.assertEqual(
            saved.completed_tests[-1].test_id,
            RESULT["test_id"],
        )
        self.assertEqual(saved.status, "investigating")
        self.assertIsNone(saved.confirmed_cause)

        history = get_history(self.db, self.case_id)

        self.assertEqual(
            [event["event_type"] for event in history],
            ["observation", "test"],
        )

        for event in history:
            self.assertEqual(
                datetime.fromisoformat(event["recorded_at"]).utcoffset(),
                timedelta(0),
            )

    def test_retry_is_safe_but_changed_payload_is_rejected(self):
        self.assertTrue(self.add("observation", OBS, "obs-1"))
        self.assertFalse(self.add("observation", OBS, "obs-1"))

        before = get_case(self.db, self.case_id)
        changed = {**OBS, "packet_loss_pct": 3}

        with self.assertRaises(ValueError):
            self.add("observation", changed, "obs-1")

        self.assertEqual(get_case(self.db, self.case_id), before)
        self.assertEqual(len(get_history(self.db, self.case_id)), 1)

    def test_repeated_test_with_new_event_id_is_preserved(self):
        self.add("test", RESULT, "test-1")
        self.add("test", RESULT, "test-2")

        saved = get_case(self.db, self.case_id)
        self.assertEqual(len(saved.completed_tests), 2)

    def test_invalid_update_changes_nothing(self):
        invalid = {**OBS, "packet_loss_pct": 101}

        with self.assertRaises(ValidationError):
            self.add("observation", invalid, "bad-1")

        self.assertEqual(
            get_case(self.db, self.case_id),
            self.original,
        )
        self.assertEqual(get_history(self.db, self.case_id), [])

    def test_history_failure_rolls_back_case_update(self):
        get_history(self.db, self.case_id)

        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute(
                    "CREATE TRIGGER reject_event "
                    "BEFORE INSERT ON case_events "
                    "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
                )

        with self.assertRaises(sqlite3.IntegrityError):
            self.add("observation", OBS, "obs-1")

        self.assertEqual(
            get_case(self.db, self.case_id),
            self.original,
        )
        self.assertEqual(get_history(self.db, self.case_id), [])

    def test_concurrent_updates_are_not_lost(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda i: self.add(
                        "observation", OBS, f"parallel-{i}"
                    ),
                    range(4),
                )
            )

        saved = get_case(self.db, self.case_id)

        self.assertEqual(
            len(saved.observations),
            len(self.original.observations) + 4,
        )
        self.assertEqual(len(get_history(self.db, self.case_id)), 4)


if __name__ == "__main__":
    unittest.main()