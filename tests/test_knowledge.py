import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.agent.rules import Decision
from backend.app.knowledge import ground_decision, load_knowledge


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

        for name in ("sources.json", "guidance.json"):
            shutil.copyfile(
                PROJECT_ROOT / "knowledge" / name,
                self.directory / name,
            )

    def read(self, name):
        return json.loads(
            (self.directory / name).read_text(encoding="utf-8-sig")
        )

    def write(self, name, data):
        (self.directory / name).write_text(
            json.dumps(data),
            encoding="utf-8",
        )

    def decision(self, action, sources=()):
        return Decision(
            case_id="knowledge_test",
            case_origin="synthetic",
            kind="report",
            action_id=action,
            message="Synthetic recommendation",
            reason="Synthetic reason",
            source_ids=list(sources),
        )

    def test_retrieval_attaches_action_specific_guidance(self):
        knowledge = load_knowledge(self.directory)

        expected = {
            "collect_performance_readings": {
                "performance_display",
            },
            "capture_affected_period": {
                "performance_display",
            },
            "compare_data_centers": {
                "data_center_selection",
                "comparable_sessions",
            },
            "compare_background_traffic": {
                "background_traffic",
                "comparable_sessions",
            },
        }

        for action, card_ids in expected.items():
            with self.subTest(action=action):
                original = self.decision(
                    action,
                    ["ea_connection_guide"],
                )
                before = original.model_dump_json()

                result = ground_decision(original, knowledge)

                self.assertEqual(
                    {card.card_id for card in result.guidance},
                    card_ids,
                )
                self.assertEqual(original.model_dump_json(), before)
                self.assertEqual(
                    result.knowledge_revision,
                    knowledge.revision,
                )

                citations = [
                    source
                    for card in result.guidance
                    for source in card.citations
                ]

                self.assertTrue(citations)
                self.assertTrue(
                    all(
                        source.url.host == "help.ea.com"
                        for source in citations
                    )
                )

    def test_verification_is_project_policy_without_external_citations(self):
        knowledge = load_knowledge(self.directory)

        for action in ("verify_improvement", "review_verification"):
            with self.subTest(action=action):
                result = ground_decision(
                    self.decision(action),
                    knowledge,
                )

                self.assertEqual(len(result.guidance), 1)
                self.assertEqual(
                    result.guidance[0].basis,
                    "project_policy",
                )
                self.assertEqual(result.guidance[0].citations, [])

    def test_unknown_action_does_not_receive_unrelated_guidance(self):
        result = ground_decision(
            self.decision("unsupported_action"),
            load_knowledge(self.directory),
        )

        self.assertEqual(result.guidance, [])

    def test_missing_sources_and_duplicate_ids_are_rejected(self):
        original_sources = self.read("sources.json")
        original_cards = self.read("guidance.json")

        missing = json.loads(json.dumps(original_cards))
        missing[0]["source_ids"] = ["nonexistent_source"]

        variants = [
            (
                original_sources + [original_sources[0]],
                original_cards,
            ),
            (
                original_sources,
                original_cards + [original_cards[0]],
            ),
            (
                original_sources,
                missing,
            ),
        ]

        for sources, cards in variants:
            with self.subTest(
                sources=len(sources),
                cards=len(cards),
            ):
                self.write("sources.json", sources)
                self.write("guidance.json", cards)

                with self.assertRaises(ValueError):
                    load_knowledge(self.directory)

    def test_unapproved_source_urls_are_rejected(self):
        urls = (
            "javascript:alert(1)",
            "http://help.ea.com/example",
            "https://example.com/example",
            "https://help.ea.com.evil.example/example",
            "https://user:password@help.ea.com/example",
        )

        for url in urls:
            with self.subTest(url=url):
                sources = self.read("sources.json")
                sources[0]["url"] = url
                self.write("sources.json", sources)

                with self.assertRaises(ValueError):
                    load_knowledge(self.directory)

    def test_attribution_rules_are_enforced(self):
        original = self.read("guidance.json")

        variants = (
            ("official_guidance", []),
            ("project_policy", ["ea_connection_guide"]),
        )

        for basis, source_ids in variants:
            with self.subTest(basis=basis):
                cards = json.loads(json.dumps(original))
                cards[0]["basis"] = basis
                cards[0]["source_ids"] = source_ids

                self.write("guidance.json", cards)

                with self.assertRaises(ValueError):
                    load_knowledge(self.directory)

    def test_rule_and_catalogue_citation_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            ground_decision(
                self.decision("compare_data_centers"),
                load_knowledge(self.directory),
            )


if __name__ == "__main__":
    unittest.main()