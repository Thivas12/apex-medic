import json
import unittest
from pathlib import Path

from backend.app.agent.rules import recommend_next_step
from backend.app.models.case import Case


ROOT = Path(__file__).resolve().parents[1]


def result(test_id, outcome):
    return {
        "test_id": test_id,
        "conditions": "Synthetic test conditions",
        "outcome": outcome,
        "notes": "Constructed development example",
    }


class NextStepTests(unittest.TestCase):
    def setUp(self):
        self.raw = (
            ROOT / "evaluation/dev_cases/packet_loss.json"
        ).read_text(encoding="utf-8-sig")

    def case(self, **changes):
        data = json.loads(self.raw)
        data.update(changes)
        return Case.model_validate_json(json.dumps(data))

    def test_reported_improvement_requests_verification(self):
        case = self.case(
            completed_tests=[
                result("compare_data_centers", "improved")
            ]
        )

        decision = recommend_next_step(case)

        self.assertEqual(decision.kind, "test")
        self.assertEqual(decision.action_id, "verify_improvement")
        self.assertEqual(decision.case_origin, "synthetic")

    def test_skips_completed_tests_then_stops(self):
        completed = [
            result("compare_data_centers", "unchanged")
        ]

        decision = recommend_next_step(
            self.case(completed_tests=completed)
        )

        self.assertEqual(
            decision.action_id,
            "compare_background_traffic",
        )

        completed.append(
            result("compare_background_traffic", "unchanged")
        )

        decision = recommend_next_step(
            self.case(completed_tests=completed)
        )

        self.assertEqual(decision.kind, "report")
        self.assertEqual(
            decision.action_id,
            "summarise_unresolved",
        )

    def test_missing_readings_and_zero_loss_differ(self):
        examples = [
            (None, "collect_performance_readings"),
            (0, "capture_affected_period"),
        ]

        for value, expected in examples:
            with self.subTest(value=value):
                observation = {
                    "source": "player_report",
                    "context": "Synthetic observation",
                    "packet_loss_pct": value,
                }

                case = self.case(observations=[observation])

                self.assertEqual(
                    recommend_next_step(case).action_id,
                    expected,
                )

    def test_bad_or_incomplete_result_requests_clarification(self):
        for outcome in ("worsened", "inconclusive"):
            with self.subTest(outcome=outcome):
                case = self.case(
                    completed_tests=[
                        result("compare_data_centers", outcome)
                    ]
                )

                decision = recommend_next_step(case)

                self.assertEqual(decision.kind, "question")
                self.assertEqual(
                    decision.action_id,
                    "clarify_test_result",
                )

    def test_unknown_procedure_is_not_repeated(self):
        case = self.case(
            completed_tests=[
                result("unreviewed_system_change", "improved")
            ]
        )

        decision = recommend_next_step(case)

        self.assertEqual(decision.kind, "report")
        self.assertEqual(
            decision.action_id,
            "review_unsupported_test",
        )

    def test_verification_does_not_set_a_diagnosis(self):
        case = self.case(
            completed_tests=[
                result("compare_data_centers", "improved"),
                result("verify_improvement", "improved"),
            ]
        )

        decision = recommend_next_step(case)

        self.assertEqual(
            decision.action_id,
            "review_verification",
        )
        self.assertEqual(case.status, "investigating")
        self.assertIsNone(case.confirmed_cause)

    def test_inactive_cases_request_no_tests(self):
        for status in ("resolved", "unresolved"):
            with self.subTest(status=status):
                decision = recommend_next_step(
                    self.case(status=status)
                )

                self.assertEqual(decision.kind, "report")

    def test_decision_is_repeatable_and_preserves_case(self):
        case = self.case()
        before = case.model_dump_json()

        first = recommend_next_step(case)
        second = recommend_next_step(case)

        self.assertEqual(first, second)
        self.assertEqual(case.model_dump_json(), before)
        self.assertEqual(
            first.source_ids,
            ["ea_connection_guide"],
        )


if __name__ == "__main__":
    unittest.main()