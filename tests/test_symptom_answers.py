import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from backend.app.agent.rules import recommend_next_step
from backend.app.db.repository import (
    append_evidence,
    create_case,
    get_case,
    get_history,
    initialise_database,
)
from backend.app.models.case import Case


ROOT = Path(__file__).resolve().parents[1]

ANSWER = {
    "question_id": "clarify_symptom",
    "symptom": "packet_loss",
}

READING = {
    "source": "player_report",
    "context": "Synthetic affected-match reading",
    "latency_ms": 55,
    "packet_loss_pct": 8,
    "fps": 144,
}


class SymptomAnswerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "cases.sqlite3"

        fixture = ROOT / "evaluation/dev_cases/packet_loss.json"
        data = json.loads(
            fixture.read_text(encoding="utf-8-sig")
        )
        data["symptom"] = "unknown"

        self.original = Case.model_validate_json(json.dumps(data))
        self.case_id = self.original.case_id

        initialise_database(self.db)
        create_case(self.db, self.original)

    def answer(self, payload, event_id):
        return append_evidence(
            self.db,
            self.case_id,
            "answer",
            json.dumps(payload),
            event_id,
        )

    def test_answer_updates_symptom_and_preserves_evidence(self):
        self.assertTrue(self.answer(ANSWER, "answer-1"))

        saved = get_case(self.db, self.case_id)

        self.assertEqual(saved.symptom, "packet_loss")
        self.assertEqual(
            saved.observations,
            self.original.observations,
        )
        self.assertEqual(saved.status, "investigating")
        self.assertIsNone(saved.confirmed_cause)

        history = get_history(self.db, self.case_id)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["event_type"], "answer")
        self.assertEqual(history[0]["entry"], ANSWER)

    def test_question_answer_measurement_loop(self):
        data = self.original.model_dump(mode="json")
        data.update(case_id="empty_case", observations=[])

        case = Case.model_validate_json(json.dumps(data))
        create_case(self.db, case)

        self.assertEqual(
            recommend_next_step(case).action_id,
            "clarify_symptom",
        )

        append_evidence(
            self.db,
            case.case_id,
            "answer",
            json.dumps(ANSWER),
            "flow-answer",
        )

        saved = get_case(self.db, case.case_id)

        self.assertEqual(
            recommend_next_step(saved).action_id,
            "collect_performance_readings",
        )

        append_evidence(
            self.db,
            case.case_id,
            "observation",
            json.dumps(READING),
            "flow-reading",
        )

        saved = get_case(self.db, case.case_id)

        self.assertEqual(
            recommend_next_step(saved).action_id,
            "compare_data_centers",
        )

    def test_invalid_answers_change_nothing(self):
        invalid = [
            {**ANSWER, "symptom": "bad_network"},
            {**ANSWER, "question_id": "clarify_test_result"},
        ]

        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.answer(payload, "invalid-answer")

                self.assertEqual(
                    get_case(self.db, self.case_id),
                    self.original,
                )
                self.assertEqual(
                    get_history(self.db, self.case_id),
                    [],
                )

    def test_replayed_answer_is_safe(self):
        self.assertTrue(self.answer(ANSWER, "answer-1"))
        self.assertFalse(self.answer(ANSWER, "answer-1"))

        with self.assertRaises(ValueError):
            self.answer(
                {**ANSWER, "symptom": "high_latency"},
                "answer-1",
            )

        saved = get_case(self.db, self.case_id)

        self.assertEqual(saved.symptom, "packet_loss")
        self.assertEqual(
            len(get_history(self.db, self.case_id)),
            1,
        )

    def test_existing_classification_cannot_be_overwritten(self):
        data = self.original.model_dump(mode="json")
        data.update(
            case_id="known_case",
            symptom="packet_loss",
        )

        known = Case.model_validate_json(json.dumps(data))
        create_case(self.db, known)

        with self.assertRaises(ValueError):
            append_evidence(
                self.db,
                known.case_id,
                "answer",
                json.dumps(
                    {**ANSWER, "symptom": "high_latency"}
                ),
                "unsolicited-answer",
            )

        self.assertEqual(
            get_case(self.db, known.case_id),
            known,
        )
        self.assertEqual(
            get_history(self.db, known.case_id),
            [],
        )

    def test_competing_answers_cannot_overwrite_each_other(self):
        def submit(symptom):
            try:
                added = self.answer(
                    {**ANSWER, "symptom": symptom},
                    f"answer-{symptom}",
                )
                return symptom, added
            except ValueError:
                return symptom, False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    submit,
                    ["packet_loss", "high_latency"],
                )
            )

        winners = [
            symptom
            for symptom, accepted in results
            if accepted
        ]

        self.assertEqual(len(winners), 1)
        self.assertEqual(
            get_case(self.db, self.case_id).symptom,
            winners[0],
        )
        self.assertEqual(
            len(get_history(self.db, self.case_id)),
            1,
        )


if __name__ == "__main__":
    unittest.main()